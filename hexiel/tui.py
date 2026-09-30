"""Textual TUI: streaming chat, tool-call rendering, approval modal, context gauge."""

import json
import threading
import time

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Markdown, Static

from . import memory as memory_mod
from . import skills as skills_mod

ACCENT = "#c8b6ff"


class TurnThread(threading.Thread):
    """Runs the agentic loop in the background; events come via call_from_thread."""

    def __init__(self, app: "HexielTUI", prompt: str) -> None:
        super().__init__(daemon=True)
        self.app_ref = app
        self.prompt = prompt

    def run(self) -> None:
        session = self.app_ref.session
        app = self.app_ref.app
        session.on_event = lambda event, *p: app.call_from_thread(
            self.app_ref.handle_event, event, *p)
        session.on_approve = self.app_ref.ask_approval
        try:
            session.run_turn(self.prompt)
        except Exception as exc:  # noqa: BLE001 - surface the crash in the UI
            app.call_from_thread(self.app_ref.add_note, f"agent crashed: {exc!r}")
        finally:
            session.save()


class ApproveScreen(ModalScreen):
    """Yes / Always / No modal for non-readonly tool calls."""

    BINDINGS = [("y", "yes", "Yes"), ("n", "no", "No"), ("a", "always", "Always"),
                ("escape", "no", "No")]

    def __init__(self, tool_name: str, args_preview: str) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._args_preview = args_preview
        self.result: str | None = None
        self.done = threading.Event()

    def compose(self) -> ComposeResult:
        with Vertical(id="approve-box"):
            yield Label(f"[b]Allow {self._tool_name}?[/b]")
            yield Static(self._args_preview, id="approve-args")
            with Horizontal(id="approve-buttons"):
                yield Button("Yes  [y]", variant="primary", id="btn-yes")
                yield Button("Always allow  [a]", id="btn-always")
                yield Button("No  [n]", variant="error", id="btn-no")

    def _finish(self, verdict: str) -> None:
        self.result = verdict
        self.done.set()
        self.dismiss(verdict)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        verdict = {"btn-yes": "yes", "btn-always": "always", "btn-no": "no"}[event.button.id]
        self._finish(verdict)

    def action_yes(self) -> None:
        self._finish("yes")

    def action_no(self) -> None:
        self._finish("no")

    def action_always(self) -> None:
        self._finish("always")


class ToolLine(Static):
    """One tool invocation line: name, args preview, and result preview once done."""

    def running(self, short: str) -> None:
        self.update(f"  [yellow]~[/yellow] {short.replace('[', chr(92) + '[')} (running…)")

    def complete(self, short: str, preview: str, is_error: bool, elapsed: float) -> None:
        bullet = "[red]✗[/red]" if is_error else "[green]✓[/green]"
        snippet = "\n    ".join(
            ln.strip().replace("[", chr(92) + "[")[:160]
            for ln in preview.strip().splitlines()[:4])
        body = f"\n    {snippet}" if snippet else ""
        self.update(f"  {bullet} {short.replace('[', chr(92) + '[')} ({elapsed:.1f}s){body}")


class StatusLine(Static):
    def __init__(self, session) -> None:
        super().__init__("")
        self._session = session

    def refresh_text(self) -> None:
        try:
            tokens_now = self._session.tokens_now()
            window = self._session.model_config.context_window
            threshold = self._session.settings.compact_threshold
            pct = 100 * tokens_now // max(window, 1)
            compact_at = int(100 * threshold)
            until = max(compact_at - pct, 0)
            model = self._session.model_config.model
            from . import todo as todo_mod
            todo_line = todo_mod.summary_line(self._session.cwd)
            self.update(
                f" hexiel | {model} | ctx {pct}% ({tokens_now // 1000}k/{window // 1000}k) "
                f"| auto-compact at {compact_at}% ({until}% left) "
                f"| {'[yellow]auto-approve[/]' if self._session.auto_approve else 'approving each write'}"
                + (f" | {todo_line}" if todo_line else "")
            )
        except Exception:  # noqa: BLE001 status line must never crash the app
            self.update(" hexiel")


class HexielTUI(App):
    TITLE = "hexiel"
    BINDINGS = [
        Binding("ctrl+c", "cancel", "Cancel turn / quit", priority=True),
        Binding("ctrl+l", "clear_chat", "Clear chat", priority=True),
    ]
    CSS = """
    #chat { padding: 0 2; }
    #input-row { height: auto; padding: 0 1; dock: bottom; }
    #prompt { height: 3; border: round $accent; background: $surface; }
    StatusLine { dock: bottom; height: 1; color: $text-muted; background: $surface; }
    .user-msg { color: $accent; margin: 1 0 0 0; }
    .assistant-msg { margin: 0 0 0 0; }
    ToolLine { color: $secondary-lighten-2; background: transparent; }
    .note { color: $text-muted; text-style: italic; }
    #approve-box { width: 80; margin: 1 4; margin-top: 1; padding: 1 2;
                   border: heavy $accent; background: $surface; }
    #approve-args { height: auto; max-height: 12; padding: 1 0; color: $text; }
    #approve-buttons { height: auto; padding: 1 0 0 0; align: center middle; }
    """

    def __init__(self, session, skills, slash_skills) -> None:
        super().__init__()
        self.session = session
        self.skills = skills
        self.slash_skills = slash_skills   # lowercase name -> skill
        self._thread = None
        self._stream_wid = None
        self._stream_text = ""
        self._last_flush = 0.0
        self._tool_widgets: dict[str, ToolLine] = {}

    # ---------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Label("[b]hexiel[/b] — your codebase's guardian angel", id="banner")
        with VerticalScroll(id="chat"):
            pass
        with Horizontal(id="input-row"):
            yield Input(placeholder="Ask hexiel…  (/help for commands)", id="prompt")
        yield StatusLine(self.session)

    def on_mount(self) -> None:
        self.status = self.query_one(StatusLine)
        self.status.refresh_text()
        self.query_one("#prompt", Input).focus()
        self.set_interval(1.0, self._tick)
        self.query_one("#chat", VerticalScroll).mount(
            Label("Type your first request; /help lists commands.", classes="note"))
        self.replay_history()
        self._refresh_status()

    def replay_history(self) -> None:
        """Render an existing conversation (e.g. after -c resume) into the chat."""
        chat = self.query_one("#chat", VerticalScroll)
        pending: dict[str, ToolLine] = {}
        for msg in self.session.messages:
            role = msg["role"]
            if role == "user":
                body = msg.get("content", "")
                if body.startswith("<auto-compaction>"):
                    chat.mount(Static("— earlier history auto-compacted —", classes="note"))
                else:
                    self.add_user_msg(body[:2000])
            elif role == "assistant":
                for call in msg.get("tool_calls", []):
                    wid = ToolLine(classes="tool")
                    pending[call["id"]] = wid
                    wid.running(call_short(call))
                    chat.mount(wid)
                text = msg.get("content", "")
                if text.strip():
                    md = Markdown(text)
                    chat.mount(md)
            elif role == "tool":
                wid = pending.get(msg.get("tool_call_id", ""))
                if wid is not None:
                    call = next((c for m in self.session.messages if m.get("tool_calls", [])
                                 for c in m["tool_calls"] if c["id"] == msg.get("tool_call_id")), {})
                    wid.complete(call_short(call), msg.get("content", ""),
                                 bool(msg.get("is_error")), 0.0)
        chat.scroll_end(animate=False)

    # ---------------------------------------------------------------- input

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        text = self.maybe_command(text)
        if not text:
            return
        self.add_user_msg(text)
        self._start_turn(text)

    def maybe_command(self, text: str) -> str | None:
        if not text.startswith("/"):
            return text
        cmd, _, arg = text[1:].partition(" ")
        arg = arg.strip()
        handler = {
            "help": self.cmd_help, "clear": self.cmd_clear, "compact": self.cmd_compact,
            "model": self.cmd_model, "models": self.cmd_models, "auto": self.cmd_auto,
            "memory": self.cmd_memory, "skills": self.cmd_skills,
            "ship": self.cmd_ship, "heal": self.cmd_heal,
            "setup-gitlab": self.cmd_setup_gitlab,
            "exit": self.cmd_exit, "quit": self.cmd_exit,
        }.get(cmd)
        if cmd in self.slash_skills:
            return f'Use the "{cmd}" skill for this request: {arg or text}\n' \
                   "(invoke it with the skill tool and follow its instructions)"
        if handler:
            handler(arg)
            return None
        self.add_note(f"unknown command /{cmd} — /help to list")
        return None

    def cmd_help(self, arg: str) -> None:
        self.add_note("/help /clear /compact /model <name> /models /auto /memory /skills "
                      "/ship [title] /heal /setup-gitlab /quit — plus any skill name, e.g. /review")

    def cmd_clear(self, arg: str) -> None:
        self.session.messages = []
        self.session.last_usage = {}
        self.query_one("#chat", VerticalScroll).remove_children()
        self._refresh_status()

    def cmd_compact(self, arg: str) -> None:
        from .llm import make_provider
        provider = make_provider(self.session.model_config, self.session.settings)
        self._start_bg(lambda: self.session.compact(provider, manual=True))

    def cmd_model(self, arg: str) -> None:
        from .config import load_config, ModelConfig
        cfg = load_config()
        if not arg:
            names = ", ".join(cfg.models)
            self.add_note(f"models: {names} — /model <name> to switch, /models for live inventory")
            return
        if arg in cfg.models:
            self.session.model_config = cfg.models[arg]
        else:
            # on-the-fly profile: inherit the current openai-compatible endpoint
            base = self.session.model_config
            self.session.model_config = ModelConfig(
                name=arg, provider="openai", base_url=base.base_url, model=arg,
                context_window=base.context_window, max_output_tokens=base.max_output_tokens)
            self.add_note(f"profile '{arg}' created on the fly ({arg} @ {base.base_url})")
        self._refresh_status()

    def cmd_auto(self, arg: str) -> None:
        self.session.auto_approve = not self.session.auto_approve
        state = "enabled" if self.session.auto_approve else "disabled"
        self.add_note(f"auto-approve {state}")
        self._refresh_status()

    def cmd_memory(self, arg: str) -> None:
        body = memory_mod.index_text(self.session.cwd)
        self.query_one("#chat", VerticalScroll).mount(
            Static(body.replace("[", "\\["), classes="note"))
        self.query_one("#chat", VerticalScroll).scroll_end(animate=False)

    def cmd_skills(self, arg: str) -> None:
        for skill in self.skills:
            self.add_note(f"{skill.name} — {skill.description}")

    def cmd_exit(self, arg: str) -> None:
        self.exit()

    # ---------------------------------------------------------------- agent plumbing

    def _start_turn(self, prompt: str) -> None:
        if self._thread and self._thread.is_alive():
            self.add_note("hexiel is still working — Ctrl+C to cancel")
            return
        self._thread = TurnThread(self, prompt)
        self._thread.start()
        self._refresh_status()

    def _start_bg(self, fn) -> None:
        def bg() -> None:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                self._push(("turn_end", {"error": repr(exc)}))
                return
            self._push(("turn_end", ))
        threading.Thread(target=bg, daemon=True).start()

    def _push(self, event) -> None:
        self.call_from_thread(self.handle_event, *event)

    # callbacks used by session ------------------------------------------------

    def handle_event(self, event, *payload) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        if event == "text":
            delta = payload[0]
            self._stream_text += delta
            now = time.time()
            if self._stream_wid is None:
                self._stream_wid = Markdown("")
                chat.mount(self._stream_wid)
                chat.scroll_end(animate=False)
            if now - self._last_flush > 0.15:
                self._stream_wid.update(self._stream_text)
                self._last_flush = now
                chat.scroll_end(animate=False)
        elif event == "turn_end":
            self.finalize_stream()
            error = payload[0].get("error") if payload and isinstance(payload[0], dict) else None
            if error:
                chat.mount(Static(f"[red]provider error:[/red] {error}", classes="note"))
        elif event == "tool_call":
            call = payload[0]
            short = call_short(call)
            wid = ToolLine(classes="tool")
            self._tool_widgets[call["id"]] = wid
            wid.running(short)
            chat.mount(wid)
            chat.scroll_end(animate=False)
        elif event == "tool_result":
            call, text, is_error = payload[0], payload[1], payload[2]
            elapsed = payload[3] if len(payload) > 3 else 0.0
            wid = self._tool_widgets.pop(call["id"], None)
            if wid:
                wid.complete(call_short(call), text, is_error, elapsed)
            self._refresh_status()
        elif event == "compact_start":
            self.finalize_stream()
            chat.mount(Static("… compacting context (summarizing history) …", classes="note"))
            chat.scroll_end(animate=False)
        elif event == "compact_end":
            stats = payload[0] if payload else {}
            if stats:
                before = stats.get("before", 0) // 1000
                after = stats.get("after", 0) // 1000
                dropped = stats.get("dropped", 0)
                kept = stats.get("kept", 0)
                chat.mount(Static(f"✓ compacted: {before}k → {after}k tokens "
                                  f"({dropped} messages summarized, {kept} kept)", classes="note"))
            self._refresh_status()
        elif event == "usage":
            self._refresh_status()
        elif event == "ship_done":
            chat.mount(Static(f"[green]{payload[0]}[/green]", classes="note"))
        chat.scroll_end(animate=False)

    def finalize_stream(self) -> None:
        if self._stream_wid is not None:
            self._stream_wid.update(self._stream_text or "*(empty response)*")
            self._stream_wid = None
            self._stream_text = ""
        self.status.refresh_text()

    def add_user_msg(self, text: str) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        chat.mount(Static(f"[bold]❯[/bold] {text.replace('[', chr(92) + '[')}", classes="user-msg"))
        chat.scroll_end(animate=False)

    def add_note(self, text: str) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        chat.mount(Static(text, classes="note"))
        chat.scroll_end(animate=False)

    def _tick(self) -> None:
        if self._stream_wid is not None:
            self._stream_wid.update(self._stream_text)
        self._refresh_status()

    def _refresh_status(self) -> None:
        self.query_one(StatusLine).refresh_text()

    # approvals from the agent thread ------------------------------------------

    def cmd_models(self, arg: str) -> None:
        from .config import list_ollama_models
        live = list_ollama_models()
        lines = []
        if live:
            lines.append("Ollama inventory: " + ", ".join(live[:24]) +
                         (" …" if len(live) > 24 else ""))
        else:
            lines.append("Ollama not reachable at localhost:11434 (is it running?)")
        self.add_note("\n".join(lines))
        if live:
            self.add_note("switch with /model <name> — any Ollama name works even without "
                          "a config profile; it is created on the fly")

    def cmd_ship(self, arg: str) -> None:
        """Propose all uncommitted work as a GitLab merge request."""
        from .gitlab import detect_project, propose_change
        title = arg.strip() or "hexiel: proposed changes"
        body = ("Proposed by a hexiel session.\n\n"
                "🤖 Generated with [hexiel](https://gitlab.com/jkagidesigns1/public/applications/hexiel)")

        def work() -> None:
            try:
                project = detect_project(self.session.cwd)
                if project is None:
                    self._push(("turn_end", {"error": "no gitlab 'origin' remote in this project"}))
                    return
                result = propose_change(self.session.cwd, title, body,
                                        on_step=lambda kind, desc: self.ask_approval(
                                            f"gitlab:{kind}", {"step": desc},
                                            project=project))
                url = result.get("mr_url") or "https://gitlab.com/-/merge_requests/new"
                self._push(("ship_done", f"MR opened: {url}" if result.get("mr_iid")
                           else f"branch {result.get('branch')} pushed — open the MR in GitLab"))
            except Exception as exc:  # noqa: BLE001
                self._push(("turn_end", {"error": f"ship failed: {exc}"}))

        if self._thread and self._thread.is_alive():
            self.add_note("hexiel is still working — cannot ship now")
            return
        threading.Thread(target=work, daemon=True).start()

    def cmd_heal(self, arg: str) -> None:
        from .heal import heal_prompt
        from . import heal as heal_mod
        incidents = heal_mod.unhealed(self.session.cwd)
        text = heal_prompt(self.session.cwd)
        self.add_note(f"/heal: {len(incidents)} unhealed incident(s) — starting guided fix "
                      "(every step asks for approval; changes go to a hexiel-fix branch)")
        self._start_turn(text)

    def cmd_setup_gitlab(self, arg: str) -> None:
        from .gitlab import save_token
        if arg:
            save_token(arg)
            self.add_note("GitLab token saved to ~/.config/hexiel/auth.toml (chmod 600)")
            return
        self.add_note("Create a Personal Access Token with 'api' scope: "
                      "https://gitlab.com/-/user_settings/personal_access_tokens "
                      "then run:  /setup-gitlab <your-token>  "
                      "(or set HEXIEL_GITLAB_TOKEN env)")

    def ask_approval(self, tool_name: str, args, project=None) -> str:
        holder = {"event": threading.Event(), "result": "no"}

        def show() -> None:
            preview = json.dumps(args, indent=2)[:1400]
            self.push_screen(ApproveScreen(tool_name, preview),
                             callback=lambda verdict: _collect(verdict))

        def _collect(verdict) -> None:
            holder["result"] = verdict or "no"
            holder["event"].set()

        self.call_from_thread(show)
        holder["event"].wait()
        return holder["result"]

    def action_cancel(self) -> None:
        if self._thread and self._thread.is_alive():
            self.session.stop()
            self.add_note("canceling turn…")
            return
        if self.query_one("#prompt", Input).value:
            self.query_one("#prompt", Input).value = ""
            return
        self.exit()

    def action_clear_chat(self) -> None:
        self.cmd_clear("")


def call_short(call: dict) -> str:
    """One-line human preview of a tool call: name + first argument."""
    name = call["name"]
    args = call.get("arguments", {})
    for key in ("command", "path", "pattern", "url", "query", "name", "action", "runner"):
        if key in args:
            value = str(args[key]).replace("\n", " ⏎ ")[:80]
            return f"{name}({value})"
    blob = json.dumps(args)
    return f"{name}({blob[:80]})"