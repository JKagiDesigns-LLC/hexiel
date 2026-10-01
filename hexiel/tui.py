"""Textual TUI: streaming chat, tool-call rendering, approval modal, context gauge."""

import json
import threading
import time
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from . import memory as memory_mod
from . import skills as skills_mod

ACCENT = "#c8b6ff"
GOLD = "#d9b55a"          # halo gold for the activity line


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
            app.call_from_thread(self.app_ref.activity.stop)


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
            approve = ("[yellow]auto-approve[/]", "auto-approve") if self._session.auto_approve \
                else ("approving writes", "approving writes")
            # (markup, plain) segments in priority order; low-priority ones drop
            # first so the line always fits the window instead of running off it
            segments = [
                (model, model),
                (f"ctx {pct}% ({tokens_now // 1000}k/{window // 1000}k)",) * 2,
                approve,
                (f"compacts at {compact_at}% ({until}% left)",) * 2,
            ] + ([(todo_line, todo_line)] if todo_line else [])
            width = max(self.size.width - 4, 20) if self.size.width else 200
            shown, used = [], 0
            for markup, plain in segments:
                cost = len(plain) + (3 if shown else 0)
                if used + cost > width:
                    if not shown:   # even the model name is too long: hard-truncate it
                        shown.append(plain[:max(width - 1, 1)] + "…")
                    continue
                shown.append(markup)
                used += cost
            self.update(" " + " · ".join(shown))
        except Exception:  # noqa: BLE001 status line must never crash the app
            self.update(" hexiel")

    def on_resize(self) -> None:
        self.refresh_text()


# slash commands: (name, argument hint, what it does) — drives /help and the menu
COMMANDS = [
    ("help", "", "list commands and skills"),
    ("model", "<name>", "switch model profile (e.g. gmktec, anthropic-sonnet)"),
    ("models", "", "list models available on the Ollama server"),
    ("clear", "", "start a fresh conversation"),
    ("compact", "", "summarize history now to free up context"),
    ("auto", "", "toggle auto-approve for edits and commands"),
    ("memory", "", "show what hexiel remembers about you and this project"),
    ("skills", "", "list installed skills"),
    ("ship", "[title]", "propose your changes as a GitLab merge request"),
    ("heal", "", "fix recorded hexiel incidents and propose the fix"),
    ("setup-gitlab", "<token>", "save a GitLab access token"),
    ("exit", "", "quit hexiel"),
]


class CommandMenu(OptionList):
    """Popup above the prompt listing slash commands / skills / model profiles."""

    def __init__(self) -> None:
        super().__init__(id="cmd-menu")
        self.display = False
        self.can_focus = False
        self._items: list[tuple[str, str]] = []   # (kind, value) parallel to options

    def show_for(self, text: str, skills, profiles: dict, current: str) -> None:
        rows: list[tuple[str, str, str]] = []      # (kind, value, label)
        if text.startswith("/model "):
            q = text[len("/model "):].strip().lower()
            for name, cfg in profiles.items():
                if q in name.lower() or q in cfg.model.lower():
                    mark = " [green]● current[/]" if name == current else ""
                    rows.append(("model", name, f"[b]{name:<18}[/b] [dim]{cfg.model}[/dim]{mark}"))
        elif text.startswith("/") and " " not in text:
            q = text[1:].lower()
            cmds = [(n, a, d) for n, a, d in COMMANDS if n.startswith(q)]
            fuzzy = len(q) >= 2   # contains-matches only once a couple of letters are typed
            cmds += [(n, a, d) for n, a, d in COMMANDS if fuzzy and q in n and not n.startswith(q)]
            for n, a, d in cmds:
                pad = " " * max(22 - len(f"/{n} {a}"), 2)
                rows.append(("cmd", n, f"[b]/{n}[/b] [dim]{a}[/dim]{pad}{d}"))
            for sk in skills:
                if sk.name.lower().startswith(q) or (fuzzy and q in sk.name.lower()):
                    desc = sk.description.replace("[", "(").splitlines()[0][:70] if sk.description else ""
                    pad = " " * max(22 - len(f"/{sk.name} skill"), 2)
                    rows.append(("skill", sk.name, f"[b]/{sk.name}[/b] [dim]skill[/dim]{pad}{desc}"))
        if not rows:
            self.hide()
            return
        self.clear_options()
        self.add_options([Option(label) for _, _, label in rows])
        self._items = [(k, v) for k, v, _ in rows]
        self.highlighted = 0
        self.display = True

    def hide(self) -> None:
        self.display = False
        self._items = []

    def selected(self) -> tuple[str, str] | None:
        if not self.display or self.highlighted is None or not self._items:
            return None
        return self._items[self.highlighted]


class PromptInput(Input):
    """The prompt: drives the slash menu with ↑/↓, Tab to complete, Enter to run, Esc to close."""

    BINDINGS = [Binding("up", "menu_up", show=False), Binding("down", "menu_down", show=False),
                Binding("tab", "menu_complete", show=False), Binding("escape", "menu_close", show=False)]

    @property
    def menu(self) -> CommandMenu:
        return self.app.query_one(CommandMenu)

    def action_menu_up(self) -> None:
        if self.menu.display:
            self.menu.action_cursor_up()

    def action_menu_down(self) -> None:
        if self.menu.display:
            self.menu.action_cursor_down()

    def action_menu_close(self) -> None:
        self.menu.hide()

    def action_menu_complete(self) -> None:
        self.app.accept_menu(run=False)

    async def action_submit(self) -> None:
        if self.menu.display and self.app.accept_menu(run=True):
            return
        await super().action_submit()


class ActivityLine(Static):
    """Bottom-left "hexiel is working" line: spinner + shimmering themed verb +
    elapsed time + tokens. Hidden while idle."""

    FRAMES = "·✢✳✶✻✽✻✶✳✢"
    WAITING = ["Discerning", "Pondering", "Contemplating", "Shepherding your code",
               "Lighting the lamp", "Gathering wisdom", "Untangling the spaghetti",
               "Counting loaves and fishes", "Herding semicolons", "Polishing halos",
               "Consulting the scrolls", "Parting the sea of brackets", "Tending the vineyard",
               "Refactoring the ark", "Seeking understanding", "Keeping watch"]
    TOOLS = {"grep": "Searching the scrolls", "glob": "Searching the scrolls",
             "list": "Surveying the land", "map": "Surveying the land",
             "read": "Reading the scrolls", "file_info": "Examining",
             "write": "Inscribing", "edit": "Inscribing",
             "bash": "Running", "powershell": "Running",
             "test": "Testing the spirits", "container": "Herding containers",
             "web_search": "Seeking far and wide", "web_fetch": "Reading the web",
             "view_image": "Beholding", "image_edit": "Restoring the image",
             "memory": "Remembering", "todo": "Keeping the ledger",
             "gitlab_mr": "Preparing the offering", "skill": "Drawing on a gift"}

    def __init__(self) -> None:
        super().__init__("", id="activity")
        self.display = False
        self._phase, self._verb, self._detail = "", "", ""
        self._started = self._verb_since = 0.0
        self._chars = 0          # streamed text + thinking chars this turn
        self._frame = 0
        self._timer = None

    def start(self) -> None:
        import random
        self._started = time.time()
        self._chars = 0
        self._set("waiting", random.choice(self.WAITING))
        self.display = True
        if self._timer is None:
            self._timer = self.set_interval(0.12, self._spin)
        self._spin()

    def stop(self) -> None:
        self.display = False
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def phase(self, phase: str, detail: str = "") -> None:
        import random
        if not self.display:
            return
        if phase == self._phase and phase != "tool":
            return
        verb = {"thinking": "Pondering deeply", "writing": "Scribing",
                "approval": "Awaiting your blessing", "compacting": "Condensing the chronicles",
                "tool": self.TOOLS.get(detail, f"Running {detail}"),
                "waiting": random.choice(self.WAITING)}.get(phase, "Working")
        self._set(phase, verb)

    def add_chars(self, n: int) -> None:
        self._chars += n

    def _set(self, phase: str, verb: str) -> None:
        self._phase, self._verb, self._verb_since = phase, verb, time.time()

    def _spin(self) -> None:
        import random
        if not self.display:
            return
        self._frame += 1
        now = time.time()
        # long waits rotate the verb so the line visibly stays alive
        if self._phase == "waiting" and now - self._verb_since > 8:
            self._set("waiting", random.choice([v for v in self.WAITING if v != self._verb]))
        spin = self.FRAMES[self._frame % len(self.FRAMES)]
        # shimmer: a bright band sweeps across the verb
        verb = self._verb + "…"
        band = self._frame % (len(verb) + 8) - 4
        shimmer = "".join(
            f"[b {ACCENT}]{ch}[/]" if abs(i - band) <= 1 else f"[{ACCENT}]{ch}[/]" if abs(i - band) <= 3 else ch
            for i, ch in enumerate(verb.replace("[", "(")))
        bits = [f"{int(now - self._started)}s"]
        if self._chars:
            bits.append(f"~{self._chars // 4} tokens")
        bits.append("[b]ctrl+c[/b] to stop" if self._phase != "approval" else "[b]y[/b]/[b]a[/b]/[b]n[/b]")
        self.update(f"[{ACCENT}]{spin}[/] [{GOLD}]{shimmer}[/] [dim]({' · '.join(bits)})[/dim]")


class Banner(Static):
    """Pinned header: version + current project dir, fitted to the window width."""

    def __init__(self, cwd: str) -> None:
        super().__init__("", id="banner")
        from . import __version__
        self._version = __version__
        home = str(Path.home())
        self._path = "~" + cwd[len(home):] if cwd == home or cwd.startswith(home + "/") else cwd
        self._project = Path(cwd).name or cwd

    def on_mount(self) -> None:
        self.refresh_text()

    def on_resize(self) -> None:
        self.refresh_text()

    def refresh_text(self) -> None:
        width = max(self.size.width - 4, 20) if self.size.width else 200
        head = f"hexiel v{self._version}"
        tagline = " — your codebase's guardian angel"
        # "~/code/" dim + "myproj" bold; the parent part elides from the left first
        parent = self._path[:len(self._path) - len(self._project)]
        room = width - len(head) - len(self._project) - 5
        if len(parent) > room:
            parent = "…" + parent[-max(room - 1, 0):] if room > 4 else ""
        line = f"[b]{head}[/b]  ▸ [dim]{parent}[/dim][b]{self._project}[/b]"
        if len(head) + len(parent) + len(self._project) + 5 + len(tagline) <= width:
            line += f"[dim]{tagline}[/dim]"
        self.update(line)


class HexielTUI(App):
    TITLE = "hexiel"
    BINDINGS = [
        Binding("ctrl+c", "cancel", "Cancel turn / quit", priority=True),
        Binding("ctrl+l", "clear_chat", "Clear chat", priority=True),
    ]
    CSS = """
    Screen { layout: vertical; }
    /* plain vertical stack (no docking): docked widgets overlap in Textual,
       which hid the input's bottom border behind the status line */
    #banner { width: 100%; height: 1; padding: 0 2; background: $surface; }
    #chat { height: 1fr; padding: 0 2; }
    #activity { height: 1; padding: 0 2; }
    #cmd-menu { height: auto; max-height: 12; margin: 0 1; border: round $accent 50%;
                background: $surface; }
    #input-row { height: auto; padding: 0 1; }
    #prompt { width: 1fr; height: 3; border: round $accent; background: $surface; }
    StatusLine { width: 100%; height: 1; padding: 0 2; color: $text-muted; background: $surface; }
    .user-msg { color: $accent; margin: 1 0 0 0; }
    .assistant-msg { margin: 0 0 0 0; }
    ToolLine { color: $secondary-lighten-2; background: transparent; }
    .note { color: $text-muted; text-style: italic; }
    #approve-box { width: 100%; max-width: 90; margin: 1 2; margin-top: 1; padding: 1 2;
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
        self._think_chars = 0       # reasoning chars this step (summarized in chat)
        self._tool_widgets: dict[str, ToolLine] = {}

    # ---------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Banner(self.session.cwd)
        with VerticalScroll(id="chat"):
            pass
        yield ActivityLine()
        yield CommandMenu()
        with Horizontal(id="input-row"):
            yield PromptInput(placeholder="Ask hexiel…  (type / for commands)", id="prompt")
        yield StatusLine(self.session)

    def on_mount(self) -> None:
        self.status = self.query_one(StatusLine)
        self.activity = self.query_one(ActivityLine)
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

    def on_input_changed(self, event: Input.Changed) -> None:
        from .config import load_config
        text = event.value
        if not text.startswith("/"):
            self.query_one(CommandMenu).hide()
            return
        try:
            profiles = load_config().models if text.startswith("/model ") else {}
        except SystemExit:
            profiles = {}
        self.query_one(CommandMenu).show_for(text, self.skills, profiles,
                                             self.session.model_config.name)

    def accept_menu(self, run: bool) -> bool:
        """Apply the highlighted menu entry. Returns True when it consumed the key."""
        menu = self.query_one(CommandMenu)
        picked = menu.selected()
        if not picked:
            return False
        kind, value = picked
        prompt = self.query_one("#prompt", Input)
        takes_arg = kind == "skill" or any(n == value and a for n, a, _ in COMMANDS)
        if kind == "model":
            line, run_now = f"/model {value}", run
        elif takes_arg or not run:
            line, run_now = f"/{value} ", False
        else:
            line, run_now = f"/{value}", True
        if run_now:
            menu.hide()
            prompt.value = ""
            self.submit_text(line)
        else:
            prompt.value = line
            prompt.cursor_position = len(line)
        return True

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.input.value = ""
        self.query_one(CommandMenu).hide()
        self.submit_text(event.value)

    def submit_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
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
        lines = [f"/{n} {a}".ljust(22) + d for n, a, d in COMMANDS]
        lines.append("/<skill> <request>".ljust(22) + "run any installed skill (/skills lists them)")
        lines.append("")
        lines.append("Attach a screenshot by typing its path, e.g. @shot.png — Ctrl+C stops a turn.")
        self.add_note("\n".join(line.replace("[", "\\[") for line in lines))

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
        self.activity.start()
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
        self.activity.start()
        threading.Thread(target=bg, daemon=True).start()

    def _push(self, event) -> None:
        self.call_from_thread(self.handle_event, *event)

    # callbacks used by session ------------------------------------------------

    def handle_event(self, event, *payload) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        if event == "thinking":
            # reasoning models can think for minutes; the activity line shows progress
            self._think_chars += len(payload[0])
            self.activity.add_chars(len(payload[0]))
            self.activity.phase("thinking")
            return
        if self._think_chars and event in ("text", "tool_call", "turn_end"):
            chat.mount(Static(f"✓ thought for ~{self._think_chars // 4} tokens", classes="note"))
            self._think_chars = 0
        if event == "text":
            self.activity.add_chars(len(payload[0]))
            self.activity.phase("writing")
        elif event == "tool_call":
            self.activity.phase("tool", payload[0].get("name", ""))
        elif event in ("tool_result", "compact_end"):
            self.activity.phase("waiting")
        elif event == "compact_start":
            self.activity.phase("compacting")
        elif event == "turn_end":
            self.activity.stop()
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
            self.activity.phase("approval")
            preview = json.dumps(args, indent=2)[:1400]
            self.push_screen(ApproveScreen(tool_name, preview),
                             callback=lambda verdict: _collect(verdict))

        def _collect(verdict) -> None:
            self.activity.phase("waiting")
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