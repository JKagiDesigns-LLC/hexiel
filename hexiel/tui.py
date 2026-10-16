"""Textual TUI: streaming chat, tool-call rendering, approval modal, context gauge."""

import json
import os
import re
import threading
import time
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.message import Message
from textual.widgets import Button, Input, Label, Markdown, OptionList, Static, TextArea
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
        session.on_event = lambda event, *p, **kw: app.call_from_thread(
            lambda: self.app_ref.handle_event(event, *p, **kw))
        session.on_approve = self.app_ref.ask_approval
        try:
            session.run_turn(self.prompt)
        except Exception as exc:  # noqa: BLE001 - surface the crash in the UI
            app.call_from_thread(self.app_ref.add_note, f"agent crashed: {exc!r}")
        finally:
            session.save()
            app.call_from_thread(self.app_ref.activity.stop)
            leftover = session.take_pending()      # typed after the last step: answer it as a follow-up turn
            if leftover:
                app.call_from_thread(self.app_ref.start_followup, "\n\n".join(leftover))


class ApproveScreen(ModalScreen):
    """Yes / Always / No modal for non-readonly tool calls."""

    BINDINGS = [("y", "yes", "Yes"), ("n", "no", "No"), ("a", "always", "Always"),
                ("escape", "no", "No")]

    TITLES = {"delegate": "Plan this with the strong model?", "delegate-plan": "Run this plan?"}

    def __init__(self, tool_name: str, args_preview: str, always_rule: str = "") -> None:
        super().__init__()
        self._tool_name = tool_name
        self._args_preview = args_preview
        self._always_rule = always_rule
        self.result: str | None = None
        self.done = threading.Event()

    def compose(self) -> ComposeResult:
        with Vertical(id="approve-box"):
            yield Label(f"[b]{self.TITLES.get(self._tool_name) or f'Allow {self._tool_name}?'}[/b]")
            if self._tool_name in self.TITLES:       # a whole plan: let it scroll instead of clipping it
                with VerticalScroll(id="approve-scroll"):
                    yield Static(self._args_preview, id="approve-args")
            else:
                yield Static(self._args_preview, id="approve-args")
            with Horizontal(id="approve-buttons"):
                yield Button("Yes  \\[y]", variant="primary", id="btn-yes")
                yield Button(f"Always allow {self._always_rule.replace('[', chr(92) + '[')}  \\[a]"
                             if self._always_rule else "Always allow  \\[a]", id="btn-always")
                yield Button("No  \\[n]", variant="error", id="btn-no")

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


class ScopeScreen(ModalScreen):
    """Ask whether a settings change is for this session or permanent."""

    BINDINGS = [("s", "pick('session')", "This session"), ("p", "pick('permanent')", "Save"),
                ("escape", "pick('')", "Cancel")]

    def __init__(self, summary: str) -> None:
        super().__init__()
        self._summary = summary

    def compose(self) -> ComposeResult:
        with Vertical(id="scope-box"):
            yield Label(f"[b]{self._summary}[/b]")
            yield Static("Keep this for just this session, or save it to config.toml?",
                         classes="note")
            with Horizontal(id="scope-buttons"):
                yield Button("This session  \\[s]", id="scope-session")
                yield Button("Save permanently  \\[p]", variant="primary", id="scope-permanent")
                yield Button("Cancel  \\[esc]", id="scope-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.action_pick(event.button.id.removeprefix("scope-").replace("cancel", ""))

    def action_pick(self, scope: str) -> None:
        self.dismiss(scope or None)


class ConfigScreen(ModalScreen):
    """/config: table of settings; Enter/click a row to pick a new value."""

    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, session) -> None:
        super().__init__()
        self.session = session
        self._rows: list[dict] = []
        self._editing: str | None = None

    def compose(self) -> ComposeResult:
        from textual.widgets import DataTable
        with Vertical(id="config-box"):
            yield Label("[b]hexiel settings[/b]  [dim]Enter/click a row to change it · Esc to close[/dim]")
            yield DataTable(id="config-table", cursor_type="row", zebra_stripes=True)
            yield OptionList(id="config-options")
            yield Input(id="config-text", placeholder="type a value and press Enter")
            yield Static("", id="config-help", classes="note")

    def on_mount(self) -> None:
        table = self.query_one("#config-table")
        table.add_columns("Setting", "Value", "Saved in config", "")
        self.query_one("#config-options").display = False
        self.query_one("#config-text").display = False
        self.reload()
        table.focus()

    def reload(self) -> None:
        from . import settings_edit
        table = self.query_one("#config-table")
        cursor = table.cursor_row
        table.clear()
        self._rows = settings_edit.rows(self.session)
        for r in self._rows:
            flag = "[yellow]this session[/]" if r["session_only"] else "[dim]saved[/]"
            table.add_row(r["label"], str(r["value"]), str(r["saved"]), flag, key=r["key"])
        if self._rows:
            table.move_cursor(row=min(cursor, len(self._rows) - 1))

    def on_data_table_row_highlighted(self, event) -> None:
        if 0 <= event.cursor_row < len(self._rows):
            self.query_one("#config-help").update(self._rows[event.cursor_row]["help"])

    def on_data_table_row_selected(self, event) -> None:
        from . import settings_edit
        row = self._rows[event.cursor_row]
        self._editing = row["key"]
        options = settings_edit.options_for(self.session, row["key"])
        if options is None:
            box = self.query_one("#config-text")
            box.value = str(row["value"] or "")
            box.display = True
            box.focus()
            return
        menu = self.query_one("#config-options")
        menu.clear_options()
        menu.add_options([Option(("● " if o == row["value"] else "  ") + str(o)) for o in options])
        menu._values = options
        menu.highlighted = options.index(row["value"]) if row["value"] in options else 0
        menu.display = True
        menu.focus()

    def on_option_list_option_selected(self, event) -> None:
        menu = self.query_one("#config-options")
        self._confirm(menu._values[event.option_index])

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._confirm(event.value)

    def _confirm(self, value) -> None:
        from . import settings_edit
        key = self._editing
        self.query_one("#config-options").display = False
        self.query_one("#config-text").display = False
        self.query_one("#config-table").focus()
        if key is None:
            return
        label = settings_edit.FIELD_BY_KEY[key]["label"]

        def done(scope) -> None:
            if not scope:
                return
            try:
                msg = settings_edit.apply(self.session, key, value, scope)
            except Exception as exc:  # noqa: BLE001 - show, don't crash the TUI
                msg = f"[red]could not change {label}: {exc}[/]"
            self.query_one("#config-help").update(msg)
            self.reload()
            self.app._refresh_status()

        self.app.push_screen(ScopeScreen(f"{label} → {value}"), callback=done)

    def action_close(self) -> None:
        if self.query_one("#config-options").display or self.query_one("#config-text").display:
            self.query_one("#config-options").display = False
            self.query_one("#config-text").display = False
            self.query_one("#config-table").focus()
            return
        self.dismiss(None)


class ModelPicker(ModalScreen):
    """Searchable list of every model reachable *right now* — each provider is
    queried live when the picker opens, results fill in as they answer."""

    BINDINGS = [("escape", "close", "Close"), ("down", "focus_list", "List")]

    def __init__(self, current: str) -> None:
        super().__init__()
        self._pk_current = current
        self._pk_groups: dict[str, list] = {}       # endpoint label -> Found list
        self._pk_errors: dict[str, str] = {}
        self._pk_pending = 0
        self._pk_rows: list = []                    # Found | None (header rows)

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Label("[b]Switch model[/b]  [dim]type to search · ↑/↓ · Enter to choose · Esc to close[/dim]")
            yield Input(placeholder="search models, e.g. sonnet, qwen, gemini…", id="picker-search")
            yield OptionList(id="picker-list")
            yield Static("", id="picker-status", classes="note")

    def on_mount(self) -> None:
        from .config import load_config
        from . import model_catalog
        self.query_one("#picker-search").focus()
        cfg = load_config()
        eps = model_catalog.endpoints(cfg)
        self._pk_pending = len(eps)
        self._pk_status()

        def work() -> None:
            model_catalog.discover(cfg, lambda ep, found, err: self.app.call_from_thread(
                self._pk_arrived, ep, found, err))
        threading.Thread(target=work, daemon=True).start()

    def _pk_arrived(self, ep, found, err) -> None:
        self._pk_pending -= 1
        if err:
            self._pk_errors[ep.label] = err
        else:
            self._pk_groups[ep.label] = found
        self._pk_status()
        self._pk_render()

    def _pk_status(self) -> None:
        n = sum(len(v) for v in self._pk_groups.values())
        bits = [f"{n} model(s) available now"]
        if self._pk_pending:
            bits.append(f"checking {self._pk_pending} provider(s)…")
        for label, err in self._pk_errors.items():
            bits.append(f"[red]✗ {label}: {err.replace('[', chr(92) + '[')}[/]")
        self.query_one("#picker-status").update("  ·  ".join(bits))

    def on_input_changed(self, event: Input.Changed) -> None:
        self._pk_render()

    @staticmethod
    def _fuzzy(query: str, text: str) -> bool:
        it = iter(text.lower())
        return all(ch in it for ch in query.lower().replace(" ", ""))

    def _pk_render(self) -> None:
        query = self.query_one("#picker-search", Input).value.strip()
        lst = self.query_one("#picker-list", OptionList)
        lst.clear_options()
        self._pk_rows = []
        options = []
        def order(label):          # current model's provider first, then local servers
            group = self._pk_groups[label]
            has_current = any(f.profile == self._pk_current for f in group)
            return (not has_current, "Ollama" not in label, label)
        for label in sorted(self._pk_groups, key=order):
            matches = [f for f in self._pk_groups[label]
                       if not query or self._fuzzy(query, f"{f.model} {label} {f.profile}")]
            if not matches:
                continue
            options.append(Option(f"[b]{label}[/b]", disabled=True))
            self._pk_rows.append(None)
            for f in sorted(matches, key=lambda f: (not f.profile, f.model)):
                mark = " [green]● current[/]" if f.profile and f.profile == self._pk_current else ""
                prof = f"  [dim]profile {f.profile}[/dim]" if f.profile else ""
                detail = f"  [dim]{f.detail}[/dim]" if f.detail else ""
                options.append(Option(f"   {f.model.replace('[', chr(92) + '[')}{detail}{prof}{mark}"))
                self._pk_rows.append(f)
        lst.add_options(options)
        first = next((i for i, r in enumerate(self._pk_rows) if r is not None), None)
        if first is not None:
            lst.highlighted = first

    def action_focus_list(self) -> None:
        self.query_one("#picker-list").focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        lst = self.query_one("#picker-list", OptionList)
        if lst.highlighted is not None:
            self._pk_choose(lst.highlighted)

    def on_option_list_option_selected(self, event) -> None:
        self._pk_choose(event.option_index)

    def _pk_choose(self, index: int) -> None:
        if 0 <= index < len(self._pk_rows) and self._pk_rows[index] is not None:
            self.dismiss(self._pk_rows[index])

    def action_close(self) -> None:
        self.dismiss(None)


class ReviewScreen(ModalScreen):
    """lazygit-style review of a self-heal fix: tick the files to include, read
    each diff, then send the fix to hexiel, keep it locally, or discard it."""

    BINDINGS = [("space", "toggle", "Include/exclude file"), ("a", "toggle_all", "All/none"),
                ("s", "pick('send')", "Send to hexiel"), ("k", "pick('keep')", "Keep just for me"),
                ("d", "pick('discard')", "Discard"), ("escape", "pick('later')", "Decide later")]

    ICON = {"modified": "[yellow]M[/]", "added": "[green]A[/]", "deleted": "[red]D[/]"}

    def __init__(self, changes, summary: str) -> None:
        super().__init__()
        self.changes = changes
        self._summary = summary

    def compose(self) -> ComposeResult:
        with Vertical(id="review-box"):
            yield Label("[b]Review the self-heal fix[/b]  [dim]space include/exclude · a all/none · "
                        "↑/↓ files · s send · k keep · d discard · esc later[/dim]")
            yield Static(self._summary, id="review-summary", classes="note")
            with Horizontal(id="review-panes"):
                yield OptionList(id="review-files")
                with VerticalScroll(id="review-diff"):
                    yield Static("", id="review-diff-body")
            yield Static("", id="review-count", classes="note")
            with Horizontal(id="review-buttons"):
                yield Button("Send to hexiel (and keep)  \\[s]", variant="primary", id="review-send")
                yield Button("Keep just for me  \\[k]", id="review-keep")
                yield Button("Discard  \\[d]", variant="error", id="review-discard")
                yield Button("Decide later  \\[esc]", id="review-later")

    def on_mount(self) -> None:
        self._render_files(0)
        self.query_one("#review-files").focus()

    def _render_files(self, highlight: int) -> None:
        files = self.query_one("#review-files", OptionList)
        files.clear_options()
        files.add_options([Option(f"{'☑' if c.selected else '☐'} {self.ICON.get(c.status, '?')} "
                                  f"{c.path.replace('[', chr(92) + '[')}  "
                                  f"[green]+{c.added}[/] [red]−{c.removed}[/]") for c in self.changes])
        files.highlighted = highlight
        n = sum(c.selected for c in self.changes)
        self.query_one("#review-count").update(f"{n} of {len(self.changes)} file(s) included")
        self._show_diff(highlight)

    def _show_diff(self, index: int | None) -> None:
        from rich.syntax import Syntax
        if index is None or not (0 <= index < len(self.changes)):
            return
        diff = self.changes[index].diff or "(binary or empty change)"
        self.query_one("#review-diff-body").update(
            Syntax(diff, "diff", theme="monokai", background_color="default", word_wrap=True))

    def on_option_list_option_highlighted(self, event) -> None:
        if event.option_list.id == "review-files":
            self._show_diff(event.option_index)

    def on_option_list_option_selected(self, event) -> None:
        self.action_toggle()

    def action_toggle(self) -> None:
        index = self.query_one("#review-files", OptionList).highlighted
        if index is not None:
            self.changes[index].selected = not self.changes[index].selected
            self._render_files(index)

    def action_toggle_all(self) -> None:
        target = not all(c.selected for c in self.changes)
        for c in self.changes:
            c.selected = target
        self._render_files(self.query_one("#review-files", OptionList).highlighted or 0)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.action_pick(event.button.id.removeprefix("review-"))

    def action_pick(self, choice: str) -> None:
        chosen = [c for c in self.changes if c.selected]
        if choice in ("send", "keep") and not chosen:
            self.query_one("#review-count").update("[red]select at least one file (space)[/]")
            return
        self.dismiss((choice, chosen))


class RestartScreen(ModalScreen):
    """After an update: restart now (resumes this conversation) or later."""

    BINDINGS = [("r", "pick('restart')", "Restart"), ("escape", "pick('')", "Later")]

    def __init__(self, version: str, headline: str = "") -> None:
        super().__init__()
        self._version = version
        self._headline = headline or f"hexiel {version} installed"

    def compose(self) -> ComposeResult:
        with Vertical(id="scope-box"):
            yield Label(f"[b]{self._headline}[/b]")
            yield Static("Restart now to use it? Your conversation is saved and resumes "
                         "automatically.", classes="note")
            with Horizontal(id="scope-buttons"):
                yield Button("Restart now  \\[r]", variant="primary", id="restart-now")
                yield Button("Later  \\[esc]", id="restart-later")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.action_pick("restart" if event.button.id == "restart-now" else "")

    def action_pick(self, choice: str) -> None:
        self.dismiss(choice or None)


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
            model = self._session.active_model or self._session.model_config.model
            from . import todo as todo_mod
            todo_line = todo_mod.summary_line(self._session.cwd)
            approve = ("[yellow]auto-approve[/]", "auto-approve") if self._session.auto_approve \
                else ("approving writes", "approving writes")
            if getattr(self._session, "plan_mode", False):
                approve = ("[b yellow]PLAN MODE[/]", "PLAN MODE")
            # (markup, plain) segments in priority order; low-priority ones drop
            # first so the line always fits the window instead of running off it
            segments = [
                (model, model),
                (f"ctx {pct}% used ({tokens_now // 1000}k/{window // 1000}k)",) * 2,
                approve,
                (f"auto-compacts at {compact_at}%",) * 2,
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


from .commands import BUILTIN as COMMANDS  # noqa: E402 - shared with the web UI


class CommandMenu(OptionList):
    """Popup above the prompt listing slash commands / skills / model profiles."""

    def __init__(self) -> None:
        super().__init__(id="cmd-menu")
        self.display = False
        self.can_focus = False
        self._items: list[tuple[str, str]] = []   # (kind, value) parallel to options

    def show_for(self, text: str, skills, profiles: dict, current: str, custom: dict | None = None) -> None:
        rows: list[tuple[str, str, str]] = []      # (kind, value, label)
        if text.startswith("/model "):
            from .presets import PRESETS
            q = text[len("/model "):].strip().lower()
            adding = q.startswith("add")
            q = q[3:].strip() if adding else q
            if not adding:
                for name, cfg in profiles.items():
                    if q in name.lower() or q in cfg.model.lower():
                        mark = " [green]● current[/]" if name == current else ""
                        rows.append(("model", name, f"[b]{name:<18}[/b] [dim]{cfg.model}[/dim]{mark}"))
            for group, name, label, fields, _ in PRESETS:
                if name in profiles or not (q in name or q in label.lower()):
                    continue
                need = f"  [dim]needs ${fields['api_key_env']}[/dim]" if fields.get("api_key_env") else ""
                rows.append(("preset", name, f"[b]+ {name:<16}[/b] [dim]add preset ·[/dim] {label}{need}"))
        elif text.startswith("/") and " " not in text:
            q = text[1:].lower()
            cmds = [(n, a, d) for n, a, d in COMMANDS if n.startswith(q)]
            fuzzy = len(q) >= 2   # contains-matches only once a couple of letters are typed
            cmds += [(n, a, d) for n, a, d in COMMANDS if fuzzy and q in n and not n.startswith(q)]
            for n, a, d in cmds:
                pad = " " * max(22 - len(f"/{n} {a}"), 2)
                rows.append(("cmd", n, f"[b]/{n}[/b] [dim]{a}[/dim]{pad}{d}"))
            for name, cmd in (custom or {}).items():
                if name.startswith(q) or (fuzzy and q in name):
                    pad = " " * max(22 - len(f"/{name} {cmd.argument_hint}"), 2)
                    desc = cmd.description.replace("[", "(")[:70]
                    rows.append(("custom", name, f"[b]/{name}[/b] [dim]{cmd.argument_hint.replace('[', '(')}[/dim]{pad}{desc}"))
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


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200d\u2060\ufeff\u202a-\u202e\u2066-\u2069]")


def sanitize_paste(text: str) -> str:
    """Make pasted text safe for the prompt: unify newlines, drop terminal escape sequences,
    stray control characters, and invisible/bidi-override characters; keep newlines and tabs."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _ANSI_RE.sub("", text)
    text = text.replace("\x1b", "")
    text = _CTRL_RE.sub("", text)
    return text.expandtabs(4) if "\t" in text else text


class PromptInput(TextArea):
    """The prompt: multiline. Enter sends; Shift+Enter / Ctrl+J / Alt+Enter insert a newline (pasted
    newlines are kept). ↑/↓, Tab, Esc drive the slash menu while it is open."""

    class Submitted(Message):
        def __init__(self, input: "PromptInput", value: str) -> None:
            super().__init__()
            self.input = input
            self.value = value

    class ValueChanged(Message):
        def __init__(self, input: "PromptInput", value: str) -> None:
            super().__init__()
            self.input = input
            self.value = value

    BINDINGS = [Binding("up", "menu_up", show=False), Binding("down", "menu_down", show=False),
                Binding("tab", "menu_complete", show=False), Binding("escape", "menu_close", show=False),
                Binding("ctrl+v", "paste_image", show=False), Binding("alt+v", "paste_image", show=False)]

    PASTE_COLLAPSE_CHARS = 1500      # longer pastes collapse to a placeholder, expanded on send
    PASTE_COLLAPSE_LINES = 15

    def __init__(self, placeholder: str = "", **kwargs) -> None:
        self._pastes: dict[str, str] = {}
        super().__init__(placeholder=placeholder, soft_wrap=True, show_line_numbers=False,
                         tab_behavior="focus", **kwargs)

    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, text: str) -> None:
        self.text = text
        self.move_cursor(self.document.end)

    @property
    def cursor_position(self) -> int:
        return len(self.text)

    @cursor_position.setter
    def cursor_position(self, _pos: int) -> None:
        self.move_cursor(self.document.end)

    @property
    def menu(self) -> CommandMenu:
        return self.app.query_one(CommandMenu)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        event.stop()
        self.styles.height = min(self.text.count("\n") + 1, 8) + 2   # grow with the text
        self.refresh(layout=True)
        self.post_message(self.ValueChanged(self, self.text))

    async def _on_paste(self, event) -> None:
        event.stop()
        event.prevent_default()
        clean = sanitize_paste(event.text)
        if not clean.strip():                    # an image-only clipboard arrives as an empty paste
            self.action_paste_image()
            return
        lines = clean.count("\n") + 1
        if len(clean) > self.PASTE_COLLAPSE_CHARS or lines > self.PASTE_COLLAPSE_LINES:
            tag = f"[Pasted text #{len(self._pastes) + 1} +{lines} lines]"
            self._pastes[tag] = clean
            clean = tag
        self.replace(clean, *self.selection)

    def action_paste_image(self) -> None:
        from .clipboard import paste_clipboard_image
        path = paste_clipboard_image(getattr(getattr(self.app, "session", None), "cwd", None) or os.getcwd())
        if path:
            self.replace(f'"{path}" ' if " " in path else f"{path} ", *self.selection)
        else:
            self.app.notify("No image on the clipboard (needs wl-paste or xclip)", severity="warning")

    def expanded(self) -> str:
        text = self.text
        for tag, body in self._pastes.items():
            text = text.replace(tag, body)
        return text

    async def _on_key(self, event) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            await self.action_submit()
        elif event.key in ("shift+enter", "ctrl+j", "alt+enter"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
        else:
            await super()._on_key(event)

    def action_menu_up(self) -> None:
        if self.menu.display:
            self.menu.action_cursor_up()
        else:
            self.action_cursor_up()

    def action_menu_down(self) -> None:
        if self.menu.display:
            self.menu.action_cursor_down()
        else:
            self.action_cursor_down()

    def action_menu_close(self) -> None:
        self.menu.hide()

    def action_menu_complete(self) -> None:
        if self.menu.display:
            self.app.accept_menu(run=False)

    async def action_submit(self) -> None:
        if self.menu.display and self.app.accept_menu(run=True):
            return
        self.post_message(self.Submitted(self, self.expanded()))
        self._pastes.clear()


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

    def reading(self, tokens: int) -> None:
        if self.display:
            self._set("reading", f"Model is reading ~{tokens // 1000}k tokens of context")

    def countdown(self, seconds: float) -> None:
        """Rate limited: show the time until auto-resume instead of a verb."""
        self._deadline = time.time() + seconds
        self._set("limited", "Rate limited")

    def _set(self, phase: str, verb: str) -> None:
        self._phase, self._verb, self._verb_since = phase, verb, time.time()

    def _spin(self) -> None:
        import random
        if not self.display:
            return
        self._frame += 1
        now = time.time()
        if self._phase == "limited":
            left = max(int(getattr(self, "_deadline", now) - now), 0)
            h, rem = divmod(left, 3600)
            clock = (f"{h}h{rem // 60:02d}m" if h else f"{rem // 60}m{rem % 60:02d}s")
            at = time.strftime("%H:%M", time.localtime(getattr(self, "_deadline", now)))
            self.update(f"[{GOLD}]⏸[/] [{GOLD}]Rate limited — resuming automatically in {clock}[/] "
                        f"[dim](at {at} · [b]ctrl+c[/b] to stop)[/dim]")
            return
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
        self._mode = ""
        self.retarget(cwd)

    def retarget(self, cwd: str, mode: str = "") -> None:
        home = str(Path.home())
        self._path = "~" + cwd[len(home):] if cwd == home or cwd.startswith(home + "/") else cwd
        self._project = Path(cwd).name or cwd
        self._mode = mode
        if self.is_mounted:
            self.refresh_text()

    def on_mount(self) -> None:
        self.refresh_text()

    def on_resize(self) -> None:
        self.refresh_text()

    def refresh_text(self) -> None:
        width = max(self.size.width - 4, 20) if self.size.width else 200
        head = f"hexiel v{self._version}" + (f"  {self._mode}" if self._mode else "")
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
        Binding("ctrl+p", "pick_model", "Switch model", priority=True),
    ]
    CSS = """
    Screen { layout: vertical; }
    /* plain vertical stack (no docking): docked widgets overlap in Textual,
       which hid the input's bottom border behind the status line */
    #banner { width: 100%; height: 1; padding: 0 2; background: $surface; }
    #chat { height: 1fr; padding: 0 2; }
    #activity { height: 1; padding: 0 2; }
    #logo { margin: 1 0 0 0; }
    #cmd-menu { height: auto; max-height: 12; margin: 0 1; border: round $accent 50%;
                background: $surface; }
    #input-row { height: auto; padding: 0 1; }
    #prompt { width: 1fr; height: 3; border: round $accent; background: $surface; }
    StatusLine { width: 100%; height: 1; padding: 0 2; color: $text-muted; background: $surface; }
    .user-msg { color: $accent; margin: 1 0 0 0; }
    .assistant-msg { margin: 0 0 0 0; }
    ToolLine { color: $secondary-lighten-2; background: transparent; }
    .note { color: $text-muted; text-style: italic; }
    #scope-box { width: 100%; max-width: 80; height: auto; margin: 2 2; padding: 1 2;
                 border: heavy $accent; background: $surface; }
    #scope-buttons { height: auto; padding: 1 0 0 0; }
    #scope-buttons Button { margin: 0 1 0 0; }
    ScopeScreen, ConfigScreen, RestartScreen, ModelPicker { align: center middle; }
    #picker-box { width: 100%; max-width: 100; height: 80%; padding: 1 2; border: heavy $accent; background: $surface; }
    #picker-list { height: 1fr; border: round $accent 50%; }
    #picker-status { height: auto; padding: 1 0 0 0; }
    #config-box { width: 100%; max-width: 110; height: auto; max-height: 90%; padding: 1 2;
                  border: heavy $accent; background: $surface; }
    #config-table { height: auto; max-height: 16; }
    #config-options { height: auto; max-height: 10; border: round $accent; }
    #config-help { height: auto; padding: 1 0 0 0; }
    ReviewScreen { align: center middle; }
    #review-box { width: 96%; height: 92%; padding: 1 2; border: heavy $accent; background: $surface; }
    #review-panes { height: 1fr; }
    #review-files { width: 38%; height: 100%; border: round $accent 50%; }
    #review-diff { width: 1fr; height: 100%; border: round $accent 50%; padding: 0 1; }
    #review-buttons { height: auto; padding: 1 0 0 0; }
    #review-buttons Button { margin: 0 1 0 0; }
    #approve-box { width: 100%; max-width: 90; margin: 1 2; margin-top: 1; padding: 1 2;
                   border: heavy $accent; background: $surface; }
    #approve-args { height: auto; max-height: 12; padding: 1 0; color: $text; }
    #approve-scroll { height: auto; max-height: 20; }
    #approve-scroll #approve-args { max-height: 400; }
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
        self._heal = None           # active self-heal: original session, workspace, incidents
        self._heal_pending = None   # a reviewed-later fix waiting for /heal review

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
        self.query_one("#prompt", PromptInput).focus()
        self.set_interval(1.0, self._tick)
        from .brand import LOGO_WIDTH, logo_text, small_logo_text
        chat = self.query_one("#chat", VerticalScroll)
        wide = self.size.width >= LOGO_WIDTH + 6
        chat.mount(Static(logo_text() if wide else small_logo_text(), id="logo"))
        chat.mount(Label("Type your first request — / opens the command menu, "
                         "@file.png attaches a screenshot.", classes="note"))
        self.replay_history()
        self._refresh_status()
        self._inbox_notice()
        if not os.environ.get("HEXIEL_NO_UPDATE_CHECK"):
            threading.Thread(target=self._check_update, daemon=True).start()

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
                # the model's text precedes the tool calls it then makes
                text = msg.get("content", "")
                if text.strip():
                    chat.mount(Markdown(text))
                for call in msg.get("tool_calls", []):
                    wid = ToolLine(classes="tool")
                    pending[call["id"]] = wid
                    wid.running(call_short(call))
                    chat.mount(wid)
            elif role == "tool":
                wid = pending.get(msg.get("tool_call_id", ""))
                if wid is not None:
                    call = next((c for m in self.session.messages if m.get("tool_calls", [])
                                 for c in m["tool_calls"] if c["id"] == msg.get("tool_call_id")), {})
                    wid.complete(call_short(call), msg.get("content", ""),
                                 bool(msg.get("is_error")), 0.0)
        chat.scroll_end(animate=False)

    # ---------------------------------------------------------------- input

    def on_prompt_input_value_changed(self, event: PromptInput.ValueChanged) -> None:
        from .config import load_config
        text = event.value
        if not text.startswith("/") or "\n" in text:
            self.query_one(CommandMenu).hide()
            return
        try:
            profiles = load_config().models if text.startswith("/model ") else {}
        except SystemExit:
            profiles = {}
        from . import commands as commands_mod
        self.query_one(CommandMenu).show_for(text, self.skills, profiles,
                                             self.session.model_config.name,
                                             commands_mod.discover(self.session.cwd))

    def accept_menu(self, run: bool) -> bool:
        """Apply the highlighted menu entry. Returns True when it consumed the key."""
        menu = self.query_one(CommandMenu)
        picked = menu.selected()
        if not picked:
            return False
        kind, value = picked
        prompt = self.query_one("#prompt", PromptInput)
        takes_arg = kind in ("skill", "custom") or any(n == value and a for n, a, _ in COMMANDS)
        if kind == "model":
            line, run_now = f"/model {value}", run
        elif kind == "preset":
            line, run_now = f"/model add {value}", run
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

    def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
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
        if cmd in ("delegate", "solo"):       # handled by the session itself (it parses the prefix)
            return text
        handler = {
            "help": self.cmd_help, "clear": self.cmd_clear, "compact": self.cmd_compact,
            "config": self.cmd_config, "update": self.cmd_update, "mcp": self.cmd_mcp,
            "plan": self.cmd_plan, "permissions": self.cmd_permissions,
            "undo": self.cmd_undo, "usage": self.cmd_usage, "jobs": self.cmd_jobs, "inbox": self.cmd_inbox,
            "model": self.cmd_model, "models": self.cmd_models, "auto": self.cmd_auto,
            "memory": self.cmd_memory, "skills": self.cmd_skills,
            "ship": self.cmd_ship, "heal": self.cmd_heal,
            "setup-gitlab": self.cmd_setup_gitlab,
            "exit": self.cmd_exit, "quit": self.cmd_exit,
        }.get(cmd)
        from . import commands as commands_mod
        custom = commands_mod.discover(self.session.cwd)
        if cmd in custom and not handler:
            return custom[cmd].render(arg)
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
            self.action_pick_model()
            return
        if arg.startswith("add"):
            self._add_preset(arg[3:].strip())
            return
        if arg in cfg.models:
            from . import settings_edit

            def done(scope) -> None:
                if scope:
                    self.add_note(settings_edit.apply(self.session, "profile", arg, scope))
                    self._refresh_status()
            self.push_screen(ScopeScreen(f"Switch model to {arg} ({cfg.models[arg].model})"),
                             callback=done)
            return
        else:
            # on-the-fly profile: inherit the current openai-compatible endpoint
            base = self.session.model_config
            self.session.model_config = ModelConfig(
                name=arg, provider="openai", base_url=base.base_url, model=arg,
                context_window=base.context_window, max_output_tokens=base.max_output_tokens)
            self.add_note(f"profile '{arg}' created on the fly ({arg} @ {base.base_url})")
        self._refresh_status()

    def _inbox_notice(self) -> None:
        from . import jobs
        box = jobs.inbox_dir()
        if not box.exists():
            return
        seen = (box / ".seen").read_text().strip() if (box / ".seen").exists() else ""
        new = [f for f in box.glob("*.md") if f.name > seen]
        if new:
            self.add_note(f"📬 {len(new)} new scheduled-job result(s) — /inbox to read")

    def _check_update(self) -> None:
        from . import updater
        info = updater.check()
        if info["available"]:
            self.call_from_thread(
                self.add_note, f"[b]⬆ hexiel {info['latest']} is available[/b] "
                f"(you have {info['current']}) — type /update to install it and restart")

    def cmd_update(self, arg: str) -> None:
        from . import updater
        if self._thread and self._thread.is_alive():
            self.add_note("finish or Ctrl+C the current turn first, then /update")
            return

        def work() -> None:
            info = updater.check(force=True)
            if not info["available"]:
                self.call_from_thread(self.add_note, f"hexiel {info['current']} is the newest release "
                                      f"(latest on GitLab: {info['latest'] or 'unknown'})")
                return
            self.call_from_thread(self.add_note, f"updating {info['current']} → {info['latest']}…")
            ok, log = updater.apply_update()
            self.call_from_thread(self.add_note, log.replace("[", "\\["))
            if ok:
                self.call_from_thread(self._offer_restart, info["latest"])
        self.activity.start()
        threading.Thread(target=lambda: (work(), self.call_from_thread(self.activity.stop)),
                         daemon=True).start()

    def _offer_restart(self, version: str, headline: str = "") -> None:
        def done(choice) -> None:
            if choice == "restart":
                self.session.save()
                self.exit(result="restart")
        self.push_screen(RestartScreen(version, headline), callback=done)

    def cmd_jobs(self, arg: str) -> None:
        from . import jobs
        items = jobs.load()
        if not items:
            self.add_note("no scheduled jobs — just ask, e.g. \"every weekday at 8am, summarise "
                          "reports/sales.csv and flag anything unusual\"")
            return
        self.add_note("\n".join(jobs.describe(j).replace("[", "\\[") for j in items)
                      + f"\nscheduler {'installed' if jobs.installed() else 'not installed yet'}")

    def cmd_inbox(self, arg: str) -> None:
        from . import jobs
        files = sorted(jobs.inbox_dir().glob("*.md"), reverse=True)[:5] if jobs.inbox_dir().exists() else []
        if not files:
            self.add_note("the inbox is empty — scheduled job results appear here")
            return
        (jobs.inbox_dir() / ".seen").write_text(files[0].name)
        chat = self.query_one("#chat", VerticalScroll)
        for f in files:
            chat.mount(Markdown(f.read_text()[:4000]))
        chat.scroll_end(animate=False)

    def cmd_undo(self, arg: str) -> None:
        if self._thread and self._thread.is_alive():
            self.add_note("finish or Ctrl+C the current turn first, then /undo")
            return
        restored = self.session.undo()
        if not restored:
            self.add_note("nothing to undo — no file changes recorded this session")
            return
        self.add_note("↶ undid the last turn's file changes:\n" + "\n".join(restored)
                      + "\n(changes made by shell commands aren't tracked)")

    def cmd_usage(self, arg: str) -> None:
        t = self.session.usage_totals
        fresh = t["prompt_tokens"] - t["cached_tokens"]
        share = 100 * t["cached_tokens"] // max(t["prompt_tokens"], 1)
        self.add_note(f"this session: {t['requests']} request(s) · input {t['prompt_tokens']:,} tokens "
                      f"({t['cached_tokens']:,} from cache = {share}%, {fresh:,} fresh) · "
                      f"output {t['completion_tokens']:,} tokens · model {self.session.model_config.model}")

    def cmd_plan(self, arg: str) -> None:
        self.session.plan_mode = not self.session.plan_mode
        if self.session.plan_mode:
            self.add_note("📝 plan mode ON — hexiel will investigate and propose a plan without "
                          "changing anything. /plan again to apply it.")
        else:
            self.add_note("plan mode OFF — say 'go ahead' (or similar) to carry out the plan")
        self._refresh_status()

    def cmd_permissions(self, arg: str) -> None:
        r = self.session.rules
        lines = [f"{kind}: " + (", ".join(getattr(r, kind)) or "—") for kind in ("deny", "ask", "allow")]
        lines.append("add rules in config.toml [permissions] or .claude/settings.json — "
                     "e.g. allow = [\"Bash(git status:*)\"], deny = [\"Read(./.env)\"]")
        self.add_note("\n".join(line.replace("[", "\\[") for line in lines))

    def cmd_mcp(self, arg: str) -> None:
        from . import mcp_client
        if arg.startswith("add"):
            name = arg[3:].strip().lower()
            if name not in mcp_client.PRESETS:
                self.add_note("MCP presets: " + ", ".join(f"{k} ({v[0]})" for k, v in mcp_client.PRESETS.items())
                              + " — /mcp add <name>")
                return
            cfg, note = mcp_client.add_preset(name, self.session.cwd)
            self.add_note(f"added '{name}' to {self.session.cwd}/.mcp.json — starting it… ({note})")

            def work() -> None:
                started = mcp_client.start_servers([cfg])
                if started and not started[0].error:
                    self.session.mcp_servers.extend(started)
                    self.session.tools.extend(mcp_client.tool_defs(started))
                self.call_from_thread(self.add_note, "\n".join(
                    line.replace("[", "\\[") for line in mcp_client.status_lines(started)))
            threading.Thread(target=work, daemon=True).start()
            return
        servers = getattr(self.session, "mcp_servers", [])
        if not servers:
            self.add_note("no MCP servers configured — add them to <project>/.mcp.json "
                          "(Claude Code format) or [mcp.servers.NAME] in config.toml")
            return
        self.add_note("\n".join(line.replace("[", "\\[") for line in mcp_client.status_lines(servers)))

    def cmd_config(self, arg: str) -> None:
        self.push_screen(ConfigScreen(self.session))

    def action_pick_model(self) -> None:
        """Ctrl+P or /model: live, searchable list of every reachable model."""
        from . import model_catalog, settings_edit

        def chosen(found) -> None:
            if found is None:
                return
            name, fields = model_catalog.profile_for(found)

            def scoped(scope) -> None:
                if not scope:
                    return
                if fields:                      # new model: give it a profile first
                    from .config import ModelConfig
                    if scope == "permanent":
                        try:
                            settings_edit.add_profile(name, fields)
                        except ValueError:
                            pass                # a profile by that name exists: just switch
                        self.add_note(settings_edit.apply(self.session, "profile", name, scope))
                    else:
                        self.session.model_config = ModelConfig(name=name, **fields)
                        self.add_note(f"Model → {found.model} (this session only)")
                else:
                    self.add_note(settings_edit.apply(self.session, "profile", name, scope))
                self._refresh_status()
            self.push_screen(ScopeScreen(f"Switch to {found.model} ({found.endpoint.label})"),
                             callback=scoped)
        self.push_screen(ModelPicker(self.session.model_config.name), callback=chosen)

    def _add_preset(self, name: str) -> None:
        from . import presets, settings_edit
        if name not in presets.BY_NAME:
            self.add_note("unknown preset — choose one of: " + ", ".join(presets.BY_NAME))
            return
        fields = presets.as_profile(name)
        try:
            self.add_note(settings_edit.add_profile(name, fields))
        except ValueError as exc:
            self.add_note(str(exc))
            return
        note = presets.BY_NAME[name][4]
        key = fields.get("api_key_env")
        if key and not os.environ.get(key):
            note += f"  ⚠ set {key} first (e.g. in ~/.bashrc), then restart hexiel"
        self.add_note(note)

        def done(scope) -> None:
            if scope:
                self.add_note(settings_edit.apply(self.session, "profile", name, scope))
                self._refresh_status()
        self.push_screen(ScopeScreen(f"Switch to {name} ({fields['model']})"), callback=done)

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

    def start_followup(self, text: str) -> None:
        """Run messages that were queued while the previous turn was ending (already shown in the chat)."""
        if self._thread and self._thread.is_alive():
            self.set_timer(0.2, lambda: self.start_followup(text))
            return
        self._start_turn(text)

    def _start_turn(self, prompt: str) -> None:
        if self._thread and self._thread.is_alive():
            self.session.queue_message(prompt)
            self.add_note("↳ sent — hexiel will read this before its next step (Ctrl+C stops the current work)")
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

    def handle_event(self, event, *payload, **_kw) -> None:
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
        if event == "request":
            tokens = payload[0] if payload else 0
            if tokens >= 8000:       # long prompts take a while on local hardware: say so
                self.activity.reading(tokens)
            return
        if event == "empty_retry":
            self.add_note("the model returned an empty reply — nudging it to continue")
            return
        if event == "model_active":
            self.status.refresh_text()
            return
        if event == "note":
            self.finalize_stream(placeholder=False)       # text after the note starts a new block below it
            self.add_note(str(payload[0]).replace("[", "\\["))
            return
        if event == "subagent":
            self.activity.phase("tool", payload[1] if len(payload) > 1 else "task")
            return
        if event == "auto_commit":
            self.add_note(f"✓ committed {payload[0]} file(s): hexiel: {payload[1]}")
            return
        if event == "rate_limited":
            seconds, reason = payload[0], payload[1] if len(payload) > 1 else ""
            self.activity.countdown(seconds)
            self.add_note(f"⏸ rate limited — hexiel will resume on its own in ~{int(seconds // 60)}m"
                          f"{int(seconds % 60):02d}s. ({reason[:120].replace('[', chr(92) + '[')})")
            return
        if event == "model_unreachable":
            seconds, reason = payload[0], payload[1] if len(payload) > 1 else ""
            self.activity.countdown(seconds)
            self.add_note(f"⚠ model server not responding — retrying in {int(seconds)}s. "
                          f"({reason[:120].replace('[', chr(92) + '[')})")
            return
        if event == "resumed":
            self.activity.phase("waiting")
            self.add_note("▶ limit reset — resuming")
            return
        if event == "fallback":
            self._refresh_status()
            old, new, reason = payload[0], payload[1], payload[2] if len(payload) > 2 else ""
            self.add_note(f"↪ {old} is limited and won't reset soon — continuing on fallback model "
                          f"[b]{new}[/b] for this session")
            self._refresh_status()
            return
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
            elapsed = time.time() - getattr(self.activity, "_started", time.time())
            self.activity.stop()
            if elapsed > 30:
                self.notify_user("hexiel finished", f"done after {int(elapsed)}s")
            if self._heal:                      # the heal turn finished: review the fix
                self.call_after_refresh(self._heal_review)
        if event == "text":
            delta = payload[0]
            self._stream_text += delta
            now = time.time()
            if self._stream_wid is None:
                # construct with the text: an update() issued before the mount
                # completes is silently dropped by Textual
                self._stream_wid = Markdown(self._stream_text)
                chat.mount(self._stream_wid)
                chat.scroll_end(animate=False)
                self._last_flush = now
            elif now - self._last_flush > 0.15:
                self._stream_wid.update(self._stream_text)
                self._last_flush = now
                chat.scroll_end(animate=False)
        elif event == "turn_end":
            self.finalize_stream()
            error = payload[0].get("error") if payload and isinstance(payload[0], dict) else None
            if error:
                chat.mount(Static(f"[red]provider error:[/red] {error}", classes="note"))
        elif event == "tool_call":
            # seal this step's text so the next step's reply renders *below* the
            # tool calls — chronological order instead of one growing blob on top
            self.finalize_stream(placeholder=False)
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

    def finalize_stream(self, placeholder: bool = True) -> None:
        if self._stream_wid is not None:
            wid, text = self._stream_wid, self._stream_text or ("*(empty response)*" if placeholder else "")
            if wid.is_mounted:
                wid.update(text)
            else:   # still mounting: an immediate update would be dropped
                self.call_after_refresh(wid.update, text)
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

    def on_app_focus(self, event) -> None:
        self._terminal_focused = True

    def on_app_blur(self, event) -> None:
        self._terminal_focused = False

    def notify_user(self, title: str, body: str) -> None:
        """Terminal bell + desktop notification, only when you're not looking at
        hexiel (terminals that don't report focus: always)."""
        if not getattr(self.session.settings, "notify", True) or getattr(self, "_terminal_focused", False):
            return
        self.bell()
        import shutil
        import subprocess
        try:
            if shutil.which("notify-send"):
                subprocess.Popen(["notify-send", "-a", "hexiel", title, body],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif shutil.which("osascript"):
                subprocess.Popen(["osascript", "-e", f'display notification "{body}" with title "{title}"'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

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
        """/heal [what went wrong] — fix hexiel itself in an isolated copy of its
        source, then review the diff and send the fix upstream or keep it."""
        from . import heal as heal_mod
        if self._thread and self._thread.is_alive():
            self.add_note("finish or Ctrl+C the current turn first, then /heal")
            return
        if arg.strip() == "review":
            if self._heal_pending:
                self._heal = self._heal_pending
                self._heal_pending = None
                self._heal_review()
            else:
                self.add_note("no self-heal fix is waiting for review")
            return
        if self._heal:
            self.add_note("already healing — finish the current fix first")
            return
        incidents = heal_mod.unhealed(self.session.cwd)
        self.add_note(f"🔧 self-heal: {len(incidents)} recorded incident(s)"
                      + (f" + your report" if arg.strip() else "")
                      + " — preparing an isolated copy of hexiel's source…")
        original = self.session

        def work() -> None:
            from . import contribute
            try:
                ws = contribute.prepare()
            except Exception as exc:  # noqa: BLE001 - surface, don't crash
                self.call_from_thread(self.add_note, f"[red]could not prepare the heal workspace:[/] {exc}")
                return
            self.call_from_thread(self._enter_heal, original, ws, incidents, arg.strip())
        threading.Thread(target=work, daemon=True).start()

    def _enter_heal(self, original, ws, incidents, description: str) -> None:
        import sys as _sys
        from .heal import heal_prompt
        from .main import build_session
        try:
            healer = build_session(str(ws.path), original.model_config.name)
        except Exception:  # noqa: BLE001 - on-the-fly profile: fall back to defaults
            healer = build_session(str(ws.path), None)
        healer.model_config = original.model_config
        healer.auto_approve = original.auto_approve
        self._heal = {"original": original, "ws": ws, "incidents": incidents, "description": description}
        self.session = healer
        self.query_one(Banner).retarget(str(ws.path), "[b yellow]· HEAL MODE[/]")
        self.add_note(f"🔧 heal mode — working on hexiel's own source in {ws.path}. "
                      "Your project and your installed hexiel stay untouched until you review the fix.")
        self._start_turn(heal_prompt(incidents, str(ws.path), description, _sys.executable))

    def _heal_review(self) -> None:
        from . import contribute
        heal = self._heal
        if not heal:
            return

        def work() -> None:
            try:
                found = contribute.changes(heal["ws"])
            except Exception as exc:  # noqa: BLE001
                self.call_from_thread(self.add_note, f"[red]could not read the fix:[/] {exc}")
                return
            self.call_from_thread(self._show_review, found)
        threading.Thread(target=work, daemon=True).start()

    def _show_review(self, found) -> None:
        heal = self._heal
        if not found:
            self.add_note("self-heal made no changes — nothing to review; leaving heal mode")
            from . import contribute
            contribute.discard(heal["ws"])
            self._exit_heal()
            return
        total_add = sum(c.added for c in found)
        total_del = sum(c.removed for c in found)
        summary = (f"{len(found)} file(s) changed, +{total_add} −{total_del}.  Send → a merge request to the "
                   "official hexiel repo so every user gets the fix (you keep it too).  Keep → applies "
                   "to your hexiel only.")
        self.push_screen(ReviewScreen(found, summary), callback=self._heal_decide)

    def _heal_title(self) -> str:
        heal = self._heal
        what = heal["description"] or next((str(i.get("detail", "")) for i in heal["incidents"]), "")
        what = " ".join(what.split())[:70] or "fix recorded incidents"
        return f"self-heal: {what}"

    def _heal_decide(self, result) -> None:
        from . import contribute, heal as heal_mod
        heal = self._heal
        choice, chosen = result if result else ("later", [])
        if choice == "later":
            self._heal_pending = heal
            self.add_note(f"fix kept for later in {heal['ws'].path} — /heal review to decide")
            self._exit_heal()
            return
        if choice == "discard":
            contribute.discard(heal["ws"])
            self.add_note("self-heal fix discarded")
            self._exit_heal()
            return

        def work() -> None:
            applied = contribute.keep(heal["ws"], chosen)
            msg = f"applied {len(applied)} file(s) to your hexiel"
            if choice == "send":
                try:
                    body = contribute.mr_body(heal["description"], heal["incidents"], chosen)
                    sent = contribute.send(heal["ws"], chosen, self._heal_title(), body)
                    msg += (f"; sent to hexiel for review: {sent['mr_url']}  — thank you, "
                            "everyone benefits once it's merged")
                except Exception as exc:  # noqa: BLE001 - keep the fix, report the send failure
                    self.call_from_thread(self.add_note, f"[red]sending failed:[/] {exc}")
                    self._heal_pending = heal
                    self.call_from_thread(self.add_note, "the fix is applied locally; /heal review to retry sending")
                    self.call_from_thread(self._exit_heal)
                    self.call_from_thread(self._offer_restart, "", "self-heal fix applied")
                    return
            heal_mod.mark_healed(heal["original"].cwd)
            contribute.discard(heal["ws"])
            self.call_from_thread(self.add_note, msg)
            self.call_from_thread(self._exit_heal)
            self.call_from_thread(self._offer_restart, "", "self-heal fix applied")
        self.activity.start()
        threading.Thread(target=lambda: (work(), self.call_from_thread(self.activity.stop)),
                         daemon=True).start()

    def _exit_heal(self) -> None:
        heal = self._heal
        self._heal = None
        if heal:
            self.session = heal["original"]
            self.query_one(Banner).retarget(self.session.cwd)
            self._refresh_status()

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
            self.notify_user("hexiel needs your approval", f"allow {tool_name}?")
            from .permissions import always_rule
            if tool_name in ApproveScreen.TITLES and isinstance(args, dict) and "text" in args:
                preview = str(args["text"])[:1800].replace("[", "\\[")
                rule = "for this session"
            else:
                preview = json.dumps(args, indent=2)[:1400]
                rule = always_rule(tool_name, args or {})
            self.push_screen(ApproveScreen(tool_name, preview, rule),
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
        if self.query_one("#prompt", PromptInput).value:
            self.query_one("#prompt", PromptInput).value = ""
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