"""Textual TUI: streaming chat, tool-call rendering, approval modal, context gauge."""

import asyncio
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.geometry import Offset
from textual.screen import ModalScreen
from textual.message import Message
from textual.theme import Theme
from textual.widgets import Button, Input, Label, Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option
from textual.widgets.text_area import Selection

from . import memory as memory_mod
from . import skills as skills_mod
from .session import STEERING_NOTE, strip_env_note

ACCENT = "#c8b6ff"
GOLD = "#d9b55a"          # halo gold for the activity line
TEXT = "#d6dae2"
MUTED = "#7d8595"
FAINT = "#4b5263"
GREEN = "#7fc8a9"
RED = "#e06c75"
YELLOW = "#e5c07b"
BLUE = "#82aaff"
BORDER = "#262b36"
BG = "#0e1014"
PANEL = "#141821"
SIDEBAR_BG = "#11141a"

HEXIEL_THEME = Theme(
    name="hexiel",
    primary=ACCENT,
    secondary=BLUE,
    accent=GOLD,
    foreground=TEXT,
    background=BG,
    surface=PANEL,
    panel=SIDEBAR_BG,
    success=GREEN,
    warning=YELLOW,
    error=RED,
    dark=True,
)

SIDEBAR_WIDTH = 31
SIDEBAR_MIN_SCREEN = 108      # below this the sidebar starts hidden (Ctrl+B still shows it)


def stamp(ts) -> str:
    """Local HH:MM for a message timestamp; empty when the message predates them."""
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OverflowError):
        return ""


def now_stamp() -> str:
    return time.strftime("%H:%M")


def fmt_tokens(n: int) -> str:
    n = max(int(n), 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n // 1_000}k"
    return str(n)


def fit(text: str, width: int) -> str:
    """Trim a plain string to `width`, with an ellipsis when it had to be cut."""
    if width <= 0:
        return ""
    return text if len(text) <= width else text[:max(width - 1, 0)] + "…"


def meter(pct: int, width: int = 18) -> str:
    filled = max(0, min(width, round(width * pct / 100)))
    colour = GREEN if pct < 60 else YELLOW if pct < 85 else RED
    return f"[{colour}]{'━' * filled}[/][{BORDER}]{'─' * (width - filled)}[/]"


def _short_parts(short: str) -> tuple[str, str]:
    """'read(src/x.py)' -> ('read', '(src/x.py)') for a two-tone tool line."""
    name, sep, rest = short.partition("(")
    return name, f"({rest}" if sep else ""


def _git_branch(cwd: str) -> str:
    """The current branch, read straight from .git/HEAD (no subprocess, no shell)."""
    try:
        git = Path(cwd) / ".git"
        if git.is_file():                      # worktree / submodule: .git points elsewhere
            text = git.read_text().strip()
            if not text.startswith("gitdir:"):
                return ""
            git = Path(text.split(":", 1)[1].strip())
            if not git.is_absolute():
                git = Path(cwd) / git
        head = (git / "HEAD").read_text().strip()
        if head.startswith("ref: refs/heads/"):
            return head[len("ref: refs/heads/"):]
        return head[:7]
    except OSError:
        return ""


class TurnThread(threading.Thread):
    """Runs the agentic loop in the background; events arrive via HexielTUI._post."""

    def __init__(self, app: "HexielTUI", prompt: str) -> None:
        super().__init__(daemon=True)
        self.app_ref = app
        self.prompt = prompt

    def run(self) -> None:
        session = self.app_ref.session
        app = self.app_ref
        post = app._post
        session.on_event = lambda event, *p, **kw: post(app.handle_event, event, *p, **kw)
        session.on_approve = app.ask_approval
        try:
            session.run_turn(self.prompt)
        except Exception as exc:  # noqa: BLE001 - surface the crash in the UI
            post(app.add_note, f"agent crashed: {exc!r}")
        finally:
            session.save()
            post(app.activity.stop)
            leftover = session.take_pending()      # typed after the last step: answer it as a follow-up turn
            if leftover:
                post(app.start_followup, "\n\n".join(leftover))


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
                    yield Static(self._args_preview, id="approve-args", markup=False)
            else:
                yield Static(self._args_preview, id="approve-args", markup=False)
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


class RemoteLoginScreen(ModalScreen):
    """/remote-control when this machine isn't signed in to Hexiel Remote yet."""

    BINDINGS = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    RemoteLoginScreen { align: center middle; }
    #remote-login { width: 64; height: auto; padding: 1 2; border: round #c8b6ff; background: $surface; }
    #remote-login Input { margin: 1 0 0 0; }
    #remote-login Horizontal { height: auto; margin-top: 1; }
    #remote-login Button { margin-right: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="remote-login"):
            yield Label("[b]Sign in to Hexiel Remote[/b]")
            yield Label("[dim]Use your remote.hexiel.tech login. No account yet? Choose Sign up.[/dim]")
            yield Input(placeholder="email", id="rl-email")
            yield Input(placeholder="password", password=True, id="rl-password")
            with Horizontal():
                yield Button("Sign in", id="rl-go", variant="primary")
                yield Button("Sign up", id="rl-signup")
                yield Button("Cancel", id="rl-cancel")

    def on_mount(self) -> None:
        self.query_one("#rl-email").focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if event.input.id == "rl-email":
            self.query_one("#rl-password").focus()
        else:
            self._go()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "rl-go":
            self._go()
        elif event.button.id == "rl-signup":
            import webbrowser
            webbrowser.open("https://remote.hexiel.tech")
            self.query_one("#rl-email").focus()
        else:
            self.dismiss(None)

    def _go(self) -> None:
        self.dismiss((self.query_one("#rl-email").value.strip(), self.query_one("#rl-password").value))

    def action_cancel(self) -> None:
        self.dismiss(None)


class _RemoteDriver:
    """What Hexiel Remote needs from the terminal UI that owns the chat."""

    def __init__(self, tui: "HexielTUI") -> None:
        self.tui = tui

    def busy(self) -> bool:
        thread = self.tui._thread
        return bool(thread and thread.is_alive())

    def submit(self, text: str) -> None:
        self.tui._post(self.tui.submit_text, text)      # typed in the browser: shown and run here


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


class PromptScreen(ModalScreen):
    """/prompt: view and edit the system prompt; save it per session, per directory or globally."""

    BINDINGS = [("s", "save('session')", "This session"), ("d", "save('directory')", "This directory"),
                ("g", "save('global')", "Globally"), ("r", "reset", "Reset"),
                ("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    PromptScreen { align: center middle; }
    #prompt-box { width: 100%; max-width: 100; height: 85%; padding: 1 2;
                  border: round #c8b6ff; background: #141821; }
    #prompt-edit { height: 1fr; margin: 1 0 0 0; border: round #2b3040; background: #11141a; }
    #prompt-buttons { height: auto; padding: 1 0 0 0; }
    #prompt-buttons Button { margin: 0 1 0 0; }
    """

    def __init__(self, source: str, text: str) -> None:
        super().__init__()
        self._source = source
        self._text = text

    def compose(self) -> ComposeResult:
        note = ("the default prompt hexiel builds — edit it, then pick where it lives"
                if self._source == "default" else f"currently: a {self._source} override")
        with Vertical(id="prompt-box"):
            yield Label(f"[b]System prompt[/b]  [dim]{note} · Esc to cancel[/dim]")
            yield TextArea(self._text, id="prompt-edit")
            with Horizontal(id="prompt-buttons"):
                yield Button("This session  \\[s]", variant="primary", id="prompt-session")
                yield Button("This directory  \\[d]", id="prompt-dir")
                yield Button("Globally  \\[g]", id="prompt-global")
                yield Button("Reset to default  \\[r]", variant="error", id="prompt-reset")
                yield Button("Cancel  \\[esc]", id="prompt-cancel")

    def on_mount(self) -> None:
        self.query_one("#prompt-edit").focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "prompt-reset":
            self.action_reset()
        elif event.button.id == "prompt-cancel":
            self.dismiss(None)
        else:
            self.action_save(event.button.id.removeprefix("prompt-"))

    # the modal's shown text
    def shown(self) -> str:
        return self.query_one("#prompt-edit").text

    def action_save(self, scope: str) -> None:
        self.dismiss(("save", scope, self.shown()))

    def action_reset(self) -> None:
        self.dismiss(("reset", None, None))

    def action_cancel(self) -> None:
        self.dismiss(None)


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


class HistoryPicker(ModalScreen):
    """Ctrl+R: fuzzy-jump back into anything you sent this session and edit/resend it."""

    BINDINGS = [("escape", "close", "Close"), ("down", "focus_list", "List"),
                ("up", "focus_search", "Search")]

    def __init__(self, history: list[str]) -> None:
        super().__init__()
        self._hs_items = list(reversed(history))     # newest first
        self._hs_rows: list[str] = []                # parallel to the options

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Label("[b]Recent messages[/b]  [dim]type to filter · ↑/↓ · Enter to reuse · Esc to close[/dim]")
            yield Input(placeholder="filter this session's messages…", id="picker-search")
            yield OptionList(id="picker-list")

    def on_mount(self) -> None:
        self.query_one("#picker-search").focus()
        self._hs_render()

    @staticmethod
    def _fuzzy(query: str, text: str) -> bool:
        it = iter(text.lower())
        return all(ch in it for ch in query.lower().replace(" ", ""))

    def on_input_changed(self, event: Input.Changed) -> None:
        self._hs_render()

    def _hs_render(self) -> None:
        query = self.query_one("#picker-search", Input).value.strip()
        lst = self.query_one("#picker-list", OptionList)
        lst.clear_options()
        self._hs_rows = []
        for text in self._hs_items:
            if query and not (self._fuzzy(query, text)
                              or query.lower() in text.lower()):
                continue
            shown = text.replace("\n", " ").replace("[", "\\[")[:110]
            self._hs_rows.append(text)
            lst.add_options([Option(shown)])
        if self._hs_rows:
            lst.highlighted = 0

    def action_focus_list(self) -> None:
        self.query_one("#picker-list").focus()

    def action_focus_search(self) -> None:
        self.query_one("#picker-search").focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        lst = self.query_one("#picker-list", OptionList)
        idx = lst.highlighted if lst.highlighted is not None else 0
        self._hs_choose(idx)

    def on_option_list_option_selected(self, event) -> None:
        self._hs_choose(event.option_index)

    def _hs_choose(self, index: int) -> None:
        if 0 <= index < len(self._hs_rows):
            self.dismiss(self._hs_rows[index])
        else:
            self.dismiss("")

    def action_close(self) -> None:
        self.dismiss("")


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
            model_catalog.discover(cfg, lambda ep, found, err: self.app._post(
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


class AttachmentChip(Static):
    """An image or PDF the person attached or the model looked at: a clickable line that opens it in the system
    viewer (a terminal cannot show a PDF), under an inline thumbnail when the terminal can draw one."""

    def __init__(self, info: dict) -> None:
        from . import media
        super().__init__(f"  {media.label(info)}  [dim]— click to open[/dim]", classes="attachment")
        self.info = info

    def on_click(self) -> None:
        from . import media
        if not media.open_external(self.info["path"]):
            self.app.notify(f"couldn't open {self.info['name']} — it is at {self.info['path']}", severity="warning")


def thumbnail_for(info: dict):
    """An inline image widget for an attachment, or None (textual-image not installed, or nothing to draw)."""
    try:
        from textual_image.widget import Image as InlineImage
    except Exception:  # noqa: BLE001 - optional extra, and some terminals break its probe
        return None
    from . import media
    source = info["path"] if info["kind"] == "image" else media.pdf_preview(info["path"])
    if not source:
        return None
    try:
        widget = InlineImage(source, classes="thumb")
    except Exception:  # noqa: BLE001
        return None
    return widget


class ChatScroll(VerticalScroll):
    """The chat transcript, pinned to the newest messages.

    While you are at the bottom the widget stays "anchored" there: every new message, streamed
    reply and tool line follows as it arrives (Textual's compositor keeps it pinned — no per-event
    scrolling needed). Scroll up to read and the anchor is released: hexiel keeps working and
    appending below, but never yanks the view. Come back down (or click the jump button) and the
    anchor re-engages on its own."""

    def anchor(self, anchor: bool = True) -> None:
        super().anchor(anchor)
        self._emit_follow(anchor)

    def release_anchor(self) -> None:
        """A user scroll let go of the bottom; re-check after the refresh settles (a scroll that
        hits the floor, e.g. wheel-down while already at the bottom, keeps the anchor)."""
        super().release_anchor()
        self.call_after_refresh(self._recheck_follow)

    def _check_anchor(self) -> None:
        released = self._anchor_released
        super()._check_anchor()
        if released and not self._anchor_released:
            self._emit_follow(True)

    def _recheck_follow(self) -> None:
        self._check_anchor()
        self._emit_follow(self._anchored and not self._anchor_released)

    def _emit_follow(self, following: bool) -> None:
        try:
            setter = getattr(self.app, "_set_following", None)
        except Exception:  # noqa: BLE001 - not running yet: nobody to tell
            return
        if setter is not None:
            setter(following)


def jump_offset(wrap, button) -> Offset:
    """Anchor a floating overlay to the bottom-right of `wrap` (absolute widgets take no layout
    space, so showing/hiding the button never shifts the text you are reading)."""
    return Offset(max(wrap.size.width - button.size.width - 1, 0),
                  max(wrap.size.height - button.size.height - 2, 0))


class JumpBottom(Button):
    """The floating "↓ scroll to bottom" tag, shown only while the view is scrolled up.
    Clicking it snaps to the newest messages and re-engages the follow."""

    def __init__(self) -> None:
        super().__init__("↓ scroll to bottom", id="jump-bottom")
        self.can_focus = False          # clicking must never steal the keyboard from the prompt
        self.display = False

    def on_resize(self) -> None:
        self.call_after_refresh(self._pin)

    def _pin(self) -> None:
        wrap = self.parent
        if isinstance(wrap, ChatWrap) and wrap.size.width and self.size.width:
            self.offset = jump_offset(wrap, self)

    def on_button_pressed(self) -> None:
        self.app.chat.anchor()
        self.display = False


class ChatWrap(Vertical):
    """The chat area: the transcript plus the overlay button, floating so text never shifts."""

    def on_resize(self) -> None:
        for jump in self.query(JumpBottom):
            jump._pin()                  # noqa: SLF001 - pinning itself is the button's own job


class UserMessage(Static):
    """A request, with its time — quiet label, readable body, a single accent rule."""

    def __init__(self, text: str, ts: str = "") -> None:
        when = f"  [{FAINT}]· {ts}[/]" if ts else ""
        body = text.replace("[", chr(92) + "[")
        super().__init__(f"[b {ACCENT}]❯[/] [b {TEXT}]you[/]{when}\n[{TEXT}]{body}[/]",
                         classes="user-msg")


class ResponseHeader(Static):
    """The start of one assistant response: marker, name, time."""

    def __init__(self, ts: str = "") -> None:
        when = f"  [{FAINT}]· {ts}[/]" if ts else ""
        super().__init__(f"[{ACCENT}]⏺[/] [b {TEXT}]hexiel[/]{when}", classes="resp-header")


class ToolLine(Static):
    """One tool invocation: a compact Claude-Code-style line with its result folded underneath."""

    @staticmethod
    def _label(short: str) -> str:
        name, args = _short_parts(short)
        name = name.replace("[", chr(92) + "[").replace("]", chr(92) + "]")
        args = args.replace("[", chr(92) + "[").replace("]", chr(92) + "]")
        return f"[b {TEXT}]{name}[/][{MUTED}]{args}[/]"

    def running(self, short: str) -> None:
        self.update(f"  [{YELLOW}]◌[/] {self._label(short)}  [dim]running…[/]")

    def complete(self, short: str, preview: str, is_error: bool, elapsed: float) -> None:
        colour = RED if is_error else GREEN
        bullet = "✗" if is_error else "⏺"
        took = f"  [dim]{elapsed:.1f}s[/]" if elapsed >= 0.1 else ""
        lines = [ln.strip().replace("[", chr(92) + "[")[:160]
                 for ln in preview.strip().splitlines() if ln.strip()][:4]
        body = ""
        if lines:
            body = "\n     [dim]⎿[/] " + f"\n       ".join(lines)
        self.update(f"  [{colour}]{bullet}[/] {self._label(short)}{took}{body}")


class StatusLine(Static):
    """Footer: the few keys worth remembering plus the state that changes most."""

    def __init__(self, session) -> None:
        super().__init__("")
        self._session = session

    def _live_session(self):
        return getattr(self.app, "session", None) or self._session

    def refresh_text(self) -> None:
        try:
            session = self._live_session()
            tokens_now = session.tokens_now()
            window = session.model_config.context_window
            pct = 100 * tokens_now // max(window, 1)
            if getattr(session, "plan_mode", False):
                mode = f"[b {YELLOW}]PLAN MODE[/]"
            elif not session.auto_approve:
                mode = f"[{MUTED}]asks before writes[/]"
            elif session.guard_commits:
                mode = f"[{MUTED}]auto-approve · asks before commits[/]"
            else:
                mode = f"[b {RED}]AUTO-APPROVE ALL[/]"
            from . import timers as timers_mod
            timer = timers_mod.summary(session)
            from . import testrun as testrun_mod
            activity = getattr(self.app, "activity", None)        # during a turn the activity line already draws the bar
            tests = "" if getattr(activity, "display", False) else testrun_mod.summary(int(time.time() * 2), 12)
            segments = [(f"[b {GOLD}]{timer}[/]", timer)] if timer else []      # first: it is what you're watching
            if tests:
                segments.insert(0, (f"[b {GOLD}]{tests}[/]", tests))
            segments += [
                (f"[{FAINT}]ctrl+p[/] [{MUTED}]model[/]", "ctrl+p model"),
                (f"[{FAINT}]ctrl+b[/] [{MUTED}]sidebar[/]", "ctrl+b sidebar"),
                (f"[{FAINT}]/[/] [{MUTED}]commands[/]", "/ commands"),
                (mode, "mode"),
                (f"[{MUTED}]ctx {pct}%[/]", f"ctx {pct}%"),
            ]
            width = max(self.size.width - 4, 20) if self.size.width else 200
            shown, used = [], 0
            for markup, plain in segments:
                cost = len(plain) + (3 if shown else 0)
                if used + cost > width:
                    continue
                shown.append(markup)
                used += cost
            self.update(" " + f"  [{FAINT}]·[/]  ".join(shown))
        except Exception:  # noqa: BLE001 - the footer must never crash the app
            self.update(" hexiel")

    def on_resize(self) -> None:
        self.refresh_text()


def spend_rows(settings, inner: int, in_use: set) -> list[str]:
    """SPEND: this cycle's bill for the accounts paying for the model at work now (none while a local model works);
    ≈ marks an estimate, the colour how close it is to the limit. /usage lists every account."""
    from . import spend
    if not in_use:
        return []
    rows = [r for r in spend.snapshot(settings) if r["account"] in in_use]
    if not rows:
        return []
    out = ["", f"[{FAINT}]SPEND THIS CYCLE[/]"]
    warn = spend.warn_at(settings)
    for r in rows:
        mark = "" if r["source"] == "provider" else "≈"
        figure = mark + spend.money(r["usd"]) + (f" / {spend.money(r['limit'])}" if r["limit"] else "")
        if r["limit"] and r.get("limit_source") == "plan":
            figure += " plan"        # the limit comes from the provider's plan, not config.toml
        name = fit(r["account"], max(4, inner - len(figure) - 1))
        colour = TEXT
        if r["limit"]:
            share = r["usd"] / r["limit"]
            colour = RED if share >= 1 else (YELLOW if share >= warn else TEXT)
        out.append(f"[{MUTED}]{name}[/]{' ' * max(1, inner - len(name) - len(figure))}[{colour}]{figure}[/]")
        if r["limit"]:
            pct = min(999, r["pct"] or 0)
            out.append(f"{meter(min(100, pct), inner - 6)} [{colour}]{pct:>3}%[/]")
    return out


WHERE_ICONS = {"ssh": "⇄", "docker": "▣", "podman": "▣", "k8s": "☸", "lxc": "▣", "container": "▣", "wsl": "❖"}


def where_rows(inner: int) -> list[str]:
    """One chip per place hexiel is running in (container, ssh session, ...): '▣ docker  sci-dev'.
    Nothing at all for a plain local terminal."""
    from .where import where
    rows = []
    for kind, name in where():
        label = f"{WHERE_ICONS.get(kind, '▣')} {kind}"
        rows.append(f"[b {BLUE}]{label}[/] [{TEXT}]{fit(name, max(inner - len(label) - 1, 4))}[/]" if name
                    else f"[b {BLUE}]{label}[/]")
    return rows


class Sidebar(Vertical):
    """A quiet opencode-style rail: where you are, what is answering, how full the context is."""

    def __init__(self) -> None:
        super().__init__(id="sidebar")
        self._body = Static("", id="sidebar-body")
        self._keys = Static(
            f"[{FAINT}]ctrl+p[/]  [{MUTED}]model[/]\n"
            f"[{FAINT}]ctrl+b[/]  [{MUTED}]sidebar[/]\n"
            f"[{FAINT}]ctrl+l[/]  [{MUTED}]clear[/]\n"
            f"[{FAINT}]ctrl+c[/]  [{MUTED}]stop / quit[/]\n"
            f"[{FAINT}]esc esc[/] [{MUTED}]stop task[/]\n"
            f"[{FAINT}]/[/]       [{MUTED}]commands[/]",
            id="sidebar-keys")

    def compose(self) -> ComposeResult:
        yield self._body
        yield self._keys

    def on_mount(self) -> None:
        self.refresh_text()

    def refresh_text(self) -> None:
        session = getattr(self.app, "session", None)
        if session is None:
            return
        from . import __version__
        from . import todo as todo_mod
        home = str(Path.home())
        cwd = session.cwd
        path = "~" + cwd[len(home):] if cwd == home or cwd.startswith(home + "/") else cwd
        project = Path(cwd).name or cwd
        branch = _git_branch(cwd)
        model = session.active_model or session.model_config.model
        provider = session.model_config.provider
        window = session.model_config.context_window
        tokens_now = session.tokens_now()
        pct = 100 * tokens_now // max(window, 1)
        compact_at = int(100 * session.model_config.compact_share(session.settings.compact_threshold))
        if getattr(session, "plan_mode", False):
            mode = f"[b {YELLOW}]plan mode[/]"
        elif not session.auto_approve:
            mode = f"[{TEXT}]asks before writes[/]"
        elif session.guard_commits:
            mode = f"[{TEXT}]auto-approve[/] [{MUTED}]except commits[/]"
        else:
            mode = f"[b {RED}]auto-approve all[/]"
        todo_line = todo_mod.summary_line(cwd)
        inner = SIDEBAR_WIDTH - 4

        rows = [
            f"[{GOLD}]✦[/] [b {ACCENT}]hexiel[/]  [{FAINT}]v{__version__}[/]",
            "",
            f"[{FAINT}]PROJECT[/]",
            f"[b {TEXT}]{fit(project, inner)}[/]",
            f"[{MUTED}]{fit(path, inner)}[/]",
        ]
        if branch:
            rows.append(f"[{MUTED}]⎇ {fit(branch, inner - 2)}[/]")
        rows += where_rows(inner)
        rows += [
            "",
            f"[{FAINT}]MODEL[/]",
            f"[b {TEXT}]{fit(model, inner)}[/]",
            f"[{MUTED}]{fit(f'{provider} · {fmt_tokens(window)} ctx', inner)}[/]",
            "",
            f"[{FAINT}]CONTEXT[/]",
            f"{meter(pct, inner - 6)} [{MUTED}]{pct:>3}%[/]",
            f"[{MUTED}]{fmt_tokens(tokens_now)} / {fmt_tokens(window)} · compacts {compact_at}%[/]",
            "",
            f"[{FAINT}]SESSION[/]",
            mode,
        ]
        if todo_line:
            rows.append(f"[{MUTED}]{fit(todo_line, inner)}[/]")
        rows += spend_rows(session.settings, inner, session.accounts_in_use())
        self._body.update("\n".join(rows))


from .commands import BUILTIN as COMMANDS  # noqa: E402 - shared with the web UI


class CommandMenu(OptionList):
    """Popup above the prompt listing slash commands / skills / model profiles."""

    def __init__(self) -> None:
        super().__init__(id="cmd-menu")
        self.display = False
        self.can_focus = False
        self._items: list[tuple[str, str]] = []   # (kind, value) parallel to options
        self._path_token: str | None = None       # the path token the path rows expand
        self._path_comps: list[dict] = []         # the full entries (is_dir etc.) for accept

    def show_paths(self, token: str, comps: list[dict] | None) -> None:
        """Candidate paths for the token just before the cursor (complete.py built them).
        A rebuild costs a full OptionList relayout — skip it when the rows didn't change."""
        comps = comps or []
        key = (token, tuple(c["name"] for c in comps))
        if getattr(self, "_rowsig", None) == key:
            self._path_token, self._path_comps = token, comps
            return
        self._rowsig = key
        self._path_token = token
        self._path_comps = comps
        self.clear_options()
        options, items = [], []
        for c in comps:
            label = c["name"].replace("[", "\\[")
            if c["is_dir"]:
                options.append(Option(f"  [b]{label}[/b] [dim]directory[/]"))
            else:
                options.append(Option(f"  [b]{label}[/b]"))
            items.append(("path", c["name"]))
        self._items = items
        self.add_options(options)
        self.highlighted = 0
        self.display = True

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
        key = tuple(rows)                          # (kind, value, label) triples drive the rebuild
        if getattr(self, "_rowsig", None) == key:
            return                                 # same rows as the last keystroke: no relayout
        self._rowsig = key
        self.clear_options()
        self.add_options([Option(label) for _, _, label in rows])
        self._items = [(k, v) for k, v, _ in rows]
        self.highlighted = 0
        self.display = True

    def hide(self) -> None:
        if self._items or self.display:
            self.display = False
        self._items = []
        self._path_token = None
        self._rowsig = None                        # next show rebuilds even for identical rows

    def selected(self) -> tuple[str, str] | None:
        if not self.display or self.highlighted is None or not self._items:
            return None
        return self._items[self.highlighted]


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]"            # CSI (`ESC[…`)
                     r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC, BEL- or ST-terminated
                     r"|\x1b[P^_].*?(?:\x1b\\|\x9c)"        # DCS/PM/APC, e.g. kitty graphics chunks
                     r"|\x1b[@-Z\\-_]")                     # the remaining two-byte escapes
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200d\u2060\ufeff\u202a-\u202e\u2066-\u2069]")
_CHAFF_RE = re.compile(r"^(?=[^\x20\n\r]*$)(?=.*[;=])[\\?=:,;\[\]0-9iKGkOcmtvf]{1,40}$")


def sanitize_paste(text: str) -> str:
    """Make pasted text safe for the prompt: unify newlines, drop terminal escape sequences,
    stray control characters, and invisible/bidi-override characters; keep newlines and tabs."""
    had_escapes = "\x1b" in text
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _ANSI_RE.sub("", text)
    text = text.replace("\x1b", "")
    text = _CTRL_RE.sub("", text)
    # A paste that was entirely terminal chatter (kitty graphics acks, OSC replies) that our
    # regexes didn't wake up to degrades to something like `i=3602480111;0K\;52;c` — no spaces,
    # a couple dozen chars. Such a paste is the "empty" clipboard of an image-only copy.
    if had_escapes and _CHAFF_RE.fullmatch(text.strip()):
        return ""
    return text.expandtabs(4) if "\t" in text else text


def prepare_terminal_graphics() -> None:
    """Set up an inline-image-capable terminal before the app takes over stdin.

    textual_image probes TGP/Sixel support and the cell size by writing safety
    queries and reading the replies — it explicitly insists this happens *before*
    Textual owns stdin; if the first rendered thumbnail does it instead, the
    replies race with the app's input thread and get typed into the prompt.
    Also patches the TGP writer to send `q=2` (send no response at all), so
    transmitting images unless an ack hits the keyboard buffer mid-typing."""
    try:
        import textual_image.renderable.tgp as tgp
        from textual_image._terminal import probe_terminal
    except ImportError:                       # no view extra: thumbnails just aren't available
        return
    try:
        probe_terminal()
    except Exception:                         # noqa: BLE001 - probing is best effort
        pass
    send = tgp._send_tgp_message
    def quiet_send(**kwargs) -> None:
        kwargs.setdefault("q", 2)             # never ack image transmits to stdin
        return send(**kwargs)
    tgp._send_tgp_message = quiet_send


def copy_selection(app, text: str) -> None:
    """Selected text -> the clipboard: OSC 52 (works over SSH) and the system helper (wl-copy/xclip/pbcopy),
    because some terminals and multiplexers ignore OSC 52."""
    from .clipboard import copy_text
    app.copy_to_clipboard(text)
    copy_text(text)


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
                Binding("ctrl+v", "paste_image", show=False), Binding("alt+v", "paste_image", show=False),
                Binding("ctrl+r", "history_search", show=False)]

    PASTE_COLLAPSE_CHARS = 1500      # longer pastes collapse to a placeholder, expanded on send
    PASTE_COLLAPSE_LINES = 15

    def __init__(self, placeholder: str = "", **kwargs) -> None:
        self._pastes: dict[str, str] = {}
        self._copy_timer = None     # debounced select-to-copy (see _watch_selection)
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
        app = self.app
        if getattr(app, "_menu", None) is None:
            app._menu = app.query_one(CommandMenu)
        return app._menu

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        event.stop()
        height = min(self.text.count("\n") + 1, 8) + 2   # grow with the text
        if self.styles.height != height:
            self.styles.height = height                   # reflows only when the height changed
        self.post_message(self.ValueChanged(self, self.text))

    async def _on_paste(self, event) -> None:
        event.stop()
        event.prevent_default()
        clean = sanitize_paste(event.text)
        if not clean.strip():                    # an image-only clipboard arrives as an empty paste
            self.action_paste_image()
            return
        self._insert_paste(clean)

    def _insert_paste(self, clean: str) -> None:
        lines = clean.count("\n") + 1
        if len(clean) > self.PASTE_COLLAPSE_CHARS or lines > self.PASTE_COLLAPSE_LINES:
            tag = f"[Pasted text #{len(self._pastes) + 1} +{lines} lines]"
            self._pastes[tag] = clean
            clean = tag
        self.replace(clean, *self.selection)

    def action_paste_image(self) -> None:
        """Ctrl+V / Alt+V: paste what the system clipboard holds — an image becomes an attachment path,
        text is inserted like a terminal paste (long text collapses to a placeholder)."""
        from . import clipboard
        path = clipboard.paste_clipboard_image(getattr(getattr(self.app, "session", None), "cwd", None) or os.getcwd())
        if path:
            self.replace(f'"{path}" ' if " " in path else f"{path} ", *self.selection)
            return
        text = clipboard.clipboard_text()
        clean = sanitize_paste(text) if text else ""
        if clean.strip():
            self._insert_paste(clean)
            return
        hint = clipboard.missing_helper_hint()
        self.app.notify(hint or "Nothing to paste: the clipboard has no text or image.", severity="warning")

    def _watch_selection(self, previous_selection, selection) -> None:
        """Select-to-copy: any text selected in the prompt (mouse drag, double-click,
        shift+arrows) lands on the system clipboard (OSC 52 — over the terminal, works
        through SSH). Debounced so a drag copies the final selection, not every step."""
        super()._watch_selection(previous_selection, selection)
        if not self.selected_text:
            return
        if self._copy_timer:
            self._copy_timer.stop()
        self._copy_timer = self.set_timer(0.2, self._copy_selected)

    def _copy_selected(self) -> None:
        self._copy_timer = None
        if self.selected_text:
            copy_selection(self.app, self.selected_text)

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
        elif self._history_up():
            return
        else:
            self.action_cursor_up()

    def action_menu_down(self) -> None:
        if self.menu.display:
            self.menu.action_cursor_down()
        elif self._history_down():
            return
        else:
            self.action_cursor_down()

    # terminal-style recall of what was sent this session (see HexielTUI._history)

    def _history_up(self) -> bool:
        """↑ on the first prompt line steps back through this session's inputs. False when the
        history is empty (or the cursor is inside the text, where ↑ must move the cursor)."""
        app = self.app
        history = getattr(app, "_history", None)
        if not history or self.cursor_location[0] != 0:
            return False
        if app._hist_pos is None:
            app._hist_draft = self.text
            app._hist_pos = len(history) - 1
        elif app._hist_pos > 0:
            app._hist_pos -= 1
        self.value = history[app._hist_pos]
        return True

    def _history_down(self) -> bool:
        """↓ steps forward again, back to the draft being typed; False while not browsing."""
        app = self.app
        history = getattr(app, "_history", None)
        if app._hist_pos is None or not history:
            return False
        if app._hist_pos + 1 < len(history):
            app._hist_pos += 1
            self.value = history[app._hist_pos]
        else:                                       # past the newest: back to what was being typed
            app._hist_pos = None
            draft, app._hist_draft = app._hist_draft, ""
            self.value = draft
        return True

    def action_menu_close(self) -> None:
        if self.menu.display:
            self.menu.hide()
            return
        escape = getattr(self.app, "escape_pressed", None)
        if escape:
            escape()

    def action_menu_complete(self) -> None:
        if self.menu.display:
            self.app.accept_menu(run=False)

    def action_history_search(self) -> None:
        """Ctrl+R: pick an earlier message of this session to edit and resend."""
        app = self.app
        history = getattr(app, "_history", [])
        if not history:
            app.notify("Nothing to recall: no messages sent in this session yet.",
                       severity="warning")
            return

        def picked(value) -> None:
            if value:
                self.value = value                # the setter puts the caret at the end
                app._hist_pos = None

        app.push_screen(HistoryPicker(history), callback=picked)

    def text_before_cursor(self) -> str:
        """Everything typed up to the caret: the tail a path token is detected from."""
        row, col = self.cursor_location
        lines = self.text.splitlines()
        if row >= len(lines) and not self.text.endswith("\n"):
            return self.text
        line = lines[row][:col] if row < len(lines) else ""
        return "\n".join(lines[:row] + [line])

    def accept_path(self, token: str, value: str) -> None:
        """Replace the path token just before the caret with its completion (Tab accept); the
        caret lands at the end of what was inserted."""
        idx = self.document.get_index_from_location(self.cursor_location)
        start = idx - len(token)
        if start < 0 or self.text[start:idx] != token:
            return                              # stale menu (the text changed under it): ignore
        loc = self.document.get_location_from_index
        self.replace(value, loc(start), loc(idx), maintain_selection_offset=False)
        end = loc(start + len(value))
        self.selection = Selection(end, end)

    async def action_submit(self) -> None:
        picked = self.menu.selected() if self.menu.display else None
        if picked and picked[0] == "path":
            # Enter always sends: a live path menu never swallows it (Tab completes; Esc closes)
            self.menu.hide()
        elif self.menu.display and self.app.accept_menu(run=True):
            return
        self.post_message(self.Submitted(self, self.expanded()))
        self._pastes.clear()


class ActivityLine(Static):
    """Bottom-left "hexiel is working" line: spinner + verb + elapsed time + tokens. Hidden while idle."""

    FRAMES = "·✢✳✶✻✽✻✶✳✢"
    WAITING = ["Thinking", "Working", "Planning", "Reading the codebase",
               "Preparing changes", "Reviewing context", "Checking the details"]
    TOOLS = {"grep": "Searching", "glob": "Searching",
             "list": "Surveying", "map": "Surveying",
             "read": "Reading", "file_info": "Inspecting",
             "write": "Writing", "edit": "Editing",
             "bash": "Running", "powershell": "Running",
             "test": "Testing", "container": "Running containers",
             "web_search": "Searching the web", "web_fetch": "Fetching the web",
             "view_image": "Looking at an image", "image_edit": "Editing an image",
             "memory": "Remembering", "todo": "Updating the plan", "timer": "Setting a timer",
             "gitlab_mr": "Preparing the merge request", "skill": "Loading a skill"}

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
        verb = {"thinking": "Thinking", "writing": "Writing",
                "approval": "Waiting for your approval", "compacting": "Compacting context",
                "tool": self.TOOLS.get(detail, f"Running {detail}"),
                "waiting": random.choice(self.WAITING)}.get(phase, "Working")
        self._set(phase, verb)

    def add_chars(self, n: int) -> None:
        self._chars += n

    def reading(self, tokens: int) -> None:
        if self.display:
            self._set("reading", f"Reading ~{tokens // 1000}k tokens of context")

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
            self.update(f"[{YELLOW}]⏸[/] [{TEXT}]Rate limited[/]  [{MUTED}]resuming in {clock} "
                        f"(at {at}) ·[/] [b]ctrl+c[/b] [{MUTED}]to stop[/]")
            return
        from . import testrun
        if testrun.active():                 # a test suite is running: its progress bar is the activity
            self.update(f"[{GOLD}]{self.FRAMES[self._frame % len(self.FRAMES)]}[/] "
                        f"[b {TEXT}]{testrun.summary(self._frame // 2, 14)}[/]  [{MUTED}]· waiting costs no tokens · "
                        f"[b]ctrl+c[/b] to stop[/]")
            return
        # long waits rotate the verb so the line visibly stays alive
        if self._phase == "waiting" and now - self._verb_since > 8:
            self._set("waiting", random.choice([v for v in self.WAITING if v != self._verb]))
        spin = self.FRAMES[self._frame % len(self.FRAMES)]
        bits = [f"{int(now - self._started)}s"]
        if self._chars:
            bits.append(f"~{self._chars // 4} tokens")
        bits.append("[b]ctrl+c[/b] to stop" if self._phase != "approval" else "[b]y[/b]/[b]a[/b]/[b]n[/b]")
        self.update(f"[{GOLD}]{spin}[/] [b {TEXT}]{self._verb}[/]  "
                    f"[{MUTED}]({' · '.join(bits)})[/]")


class Banner(Static):
    """Pinned top bar: wordmark, project, branch, and any active mode."""

    def __init__(self, cwd: str) -> None:
        super().__init__("", id="banner")
        from . import __version__
        self._version = __version__
        self._mode = ""
        self.retarget(cwd)

    def retarget(self, cwd: str, mode: str = "") -> None:
        home = str(Path.home())
        self._cwd = cwd
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
        branch = _git_branch(self._cwd)
        left = (f"[b {ACCENT}]◆[/] [b {TEXT}]hexiel[/] [{FAINT}]v{self._version}[/]"
                f"   [{FAINT}]│[/]   [b {TEXT}]{fit(self._project, 28)}[/]")
        tail = f"   [{FAINT}]│[/]   [{MUTED}]⎇ {fit(branch, 22)}[/]" if branch else ""
        if self._mode:
            tail += f"   {self._mode}"
        parent = self._path[:max(len(self._path) - len(self._project), 0)]
        if len(parent) > 1 and len(left) + len(parent) + len(tail) + 8 <= width:
            left += f"  [{FAINT}]{fit(parent, width // 2)}[/]"
        self.update(left + tail)


class Welcome(Static):
    """A compact, quiet welcome card shown when the conversation is empty."""

    def __init__(self, cwd: str) -> None:
        branch = _git_branch(cwd)
        project = Path(cwd).name or cwd
        hint = f"[{MUTED}]⎇ {branch}[/]   " if branch else ""
        super().__init__(
            f"[b {ACCENT}]hexiel[/]  [{FAINT}]·[/]  [{MUTED}]your codebase's guardian angel[/]\n"
            f"{hint}[{MUTED}]{fit(cwd, 78)}[/]\n\n"
            f"[{TEXT}]Ask a question, describe a change, or type[/] [b {ACCENT}]/[/] "
            f"[{TEXT}]for commands.[/]\n\n"
            f"[{MUTED}]  ❯  explain how this project is structured[/]\n"
            f"[{MUTED}]  ❯  fix the failing tests[/]\n"
            f"[{MUTED}]  ❯  refactor src/{project}.py[/]\n\n"
            f"[{FAINT}]Enter sends · Shift+Enter adds a line · @file.png attaches an image[/]",
            id="welcome")


class HexielTUI(App):
    TITLE = "hexiel"
    BINDINGS = [
        Binding("ctrl+c", "cancel", "Cancel turn / quit", priority=True),
        Binding("ctrl+l", "clear_chat", "Clear chat", priority=True),
        Binding("ctrl+p", "pick_model", "Switch model", priority=True),
        Binding("ctrl+b", "toggle_sidebar", "Toggle sidebar", priority=True, show=False),
    ]
    CSS = """
    Screen { layout: vertical; background: #0e1014; color: #d6dae2; }
    Button {
        border: round #2b3040;
        background: #171b24;
        color: #d6dae2;
        min-width: 12;
    }
    Button:hover { border: round #c8b6ff 70%; background: #1d2230; }
    Button.-primary { border: round #c8b6ff; background: #c8b6ff; color: #0e1014; text-style: bold; }
    Button.-primary:hover { background: #d8ccff; }
    Button.-error { border: round #e06c75; background: transparent; color: #e06c75; }
    Button.-error:hover { background: #2a171c; }

    /* ---------------------------------------------------------------- shell */
    #body { height: 1fr; }
    #main { width: 1fr; height: 100%; }
    #sidebar { width: 31; height: 100%; padding: 1 1 0 1;
               background: #11141a; border-right: solid #232834; }
    #sidebar-body { height: 1fr; }
    #sidebar-keys { height: auto; padding: 1 0 0 0; border-top: solid #232834; margin-top: 1; }

    #banner { width: 100%; height: 2; padding: 0 2; content-align: left middle;
              background: #11141a; border-bottom: solid #232834; }
    #chat-wrap { height: 1fr; }
    #chat { height: 1fr; padding: 1 2 0 2; scrollbar-size-vertical: 1; }
    #jump-bottom { position: absolute; height: 1; padding: 0 1; border: none; min-width: 0;
                   background: #141821 90%; color: #7d8595; }
    #jump-bottom:hover { background: #1d2230; color: #d6dae2; }
    #jump-bottom:focus { text-style: none; }
    #activity { height: 1; padding: 0 2; margin: 0 0 1 0; }

    /* ---------------------------------------------------------------- messages */
    .user-msg { color: #e7eaf1; background: #171b24; border-left: thick #7d8cff;
                padding: 0 1; margin: 1 0 0 0; }
    .resp-header { color: #7d8595; margin: 1 0 0 1; }
    .assistant-msg { margin: 0 0 0 2; background: transparent; }
    ToolLine { color: #b7bfcd; background: transparent; margin: 0 0 0 1; }
    .note { color: #7d8595; text-style: italic; margin: 0 0 0 2; }
    .welcome { margin: 1 0 1 1; padding: 0 1; border-left: thick #2b3040; }
    .attachment { color: #7d8595; margin: 0 0 0 2; }
    .attachment:hover { color: #c8b6ff; text-style: underline; }
    .thumb { width: auto; max-width: 48; height: auto; max-height: 14; margin: 0 0 0 2; }
    .weather { color: #c8b6ff; text-style: none; border-left: thick #c8b6ff;
               padding: 0 1; margin: 1 0 1 1; }
    Markdown { background: transparent; }

    /* ---------------------------------------------------------------- prompt */
    #cmd-menu { height: auto; max-height: 12; margin: 0 2 0 2; border: round #2b3040;
                background: #141821; }
    #input-row { height: auto; padding: 0 2; }
    #prompt { width: 1fr; height: 3; border: round #2b3040; background: #151922; padding: 0 1; }
    #prompt:focus { border: round #c8b6ff; }
    StatusLine { width: 100%; height: 2; padding: 0 2; content-align: left middle;
                 color: #7d8595; background: #11141a; border-top: solid #232834; }

    /* ---------------------------------------------------------------- modals */
    ScopeScreen, ConfigScreen, RestartScreen, ModelPicker, ReviewScreen { align: center middle; }
    #scope-box { width: 100%; max-width: 80; height: auto; margin: 2 2; padding: 1 2;
                 border: round #c8b6ff; background: #141821; }
    #scope-buttons { height: auto; padding: 1 0 0 0; }
    #scope-buttons Button { margin: 0 1 0 0; }
    #picker-box { width: 100%; max-width: 100; height: 80%; padding: 1 2;
                  border: round #c8b6ff; background: #141821; }
    #picker-list { height: 1fr; border: round #2b3040 60%; background: #11141a; }
    #picker-status { height: auto; padding: 1 0 0 0; }
    #config-box { width: 100%; max-width: 110; height: auto; max-height: 90%; padding: 1 2;
                  border: round #c8b6ff; background: #141821; }
    #config-table { height: auto; max-height: 16; background: #11141a; }
    #config-options { height: auto; max-height: 10; border: round #2b3040; background: #11141a; }
    #config-help { height: auto; padding: 1 0 0 0; }
    #review-box { width: 96%; height: 92%; padding: 1 2; border: round #c8b6ff; background: #141821; }
    #review-panes { height: 1fr; }
    #review-files { width: 38%; height: 100%; border: round #2b3040 60%; background: #11141a; }
    #review-diff { width: 1fr; height: 100%; border: round #2b3040 60%; padding: 0 1; background: #11141a; }
    #review-buttons { height: auto; padding: 1 0 0 0; }
    #review-buttons Button { margin: 0 1 0 0; }
    #approve-box { width: 100%; max-width: 90; margin: 1 2; margin-top: 1; padding: 1 2;
                   border: round #c8b6ff; background: #141821; }
    #approve-args { height: auto; max-height: 12; padding: 1 0; color: #d6dae2; }
    #approve-scroll { height: auto; max-height: 20; }
    #approve-scroll #approve-args { max-height: 400; }
    #approve-buttons { height: auto; padding: 1 0 0 0; align: center middle; }
    """

    def __init__(self, session, skills, slash_skills) -> None:
        super().__init__()
        self.register_theme(HEXIEL_THEME)
        self.theme = "hexiel"
        self.session = session
        session.dismiss_approval = self._dismiss_approval
        self.skills = skills
        self.slash_skills = slash_skills   # lowercase name -> skill
        self._thread = None
        self._stream_wid = None
        self._stream_header = None
        self._stream_text = ""
        self._rendered_stream = ""  # what the Markdown widget currently shows (paint only when it changes)
        self._flush_timer = None    # coalesces streamed deltas into a few repaints a second
        self._think_chars = 0       # reasoning chars this step (summarized in chat)
        self._tool_widgets: dict[str, ToolLine] = {}
        self._heal = None           # active self-heal: original session, workspace, incidents
        self._heal_pending = None   # a reviewed-later fix waiting for /heal review
        self._watch = None          # active /watch: keeps reacting to AI:/AI? comments on save
        self._sidebar_forced: bool | None = None   # Ctrl+B overrides the responsive default
        self._esc_armed_at = 0.0    # Esc pressed while a task runs: a second Esc stops it
        self._chat = None           # cached widgets: query_one walks the whole DOM on every use
        self._menu = None
        self._following = True
        self._history: list[str] = []       # this session's inputs, newest last (↑/↓ recall)
        self._hist_pos: int | None = None   # where ↑/↓ are in the history right now
        self._hist_draft = ""               # the text being typed when ↑ was first pressed
        self._profile_cache = (0.0, {})     # /model menu: config read at most every few seconds
        self._commands_cache = (0.0, {})    # custom commands: the directory is walked at most every few seconds

    @property
    def chat(self) -> "ChatScroll":
        """The scrolling transcript (cached; a query on every streamed event walks the whole DOM)."""
        if self._chat is None:
            self._chat = self.query_one("#chat", ChatScroll)
        return self._chat

    @property
    def menu(self) -> CommandMenu:
        if self._menu is None:
            self._menu = self.query_one(CommandMenu)
        return self._menu

    def _set_following(self, following: bool) -> None:
        """The chat told us the view started/stopped following the bottom: flip the jump button."""
        if following == self._following:
            return
        self._following = following
        jump = self.query(JumpBottom)
        if jump:
            jump.first().display = not following and self.chat.max_scroll_y > 0

    # ---------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            yield Sidebar()
            with Vertical(id="main"):
                yield Banner(self.session.cwd)
                with ChatWrap(id="chat-wrap"):
                    with ChatScroll(id="chat"):
                        pass
                    yield JumpBottom()
                yield ActivityLine()
                yield CommandMenu()
                with Horizontal(id="input-row"):
                    yield PromptInput(placeholder="Ask hexiel…  (type / for commands)", id="prompt")
                yield StatusLine(self.session)

    def on_mount(self) -> None:
        self.status = self.query_one(StatusLine)
        self.sidebar = self.query_one(Sidebar)
        self.activity = self.query_one(ActivityLine)
        self.status.refresh_text()
        self.sidebar.refresh_text()
        self.query_one("#prompt", PromptInput).focus()
        self.set_interval(1.0, self._tick)
        self._apply_sidebar_visibility()
        chat = self.chat
        chat.mount(Welcome(self.session.cwd))
        self.replay_history()
        chat.anchor()               # follow the newest messages until you scroll up
        self._refresh_status()
        self._inbox_notice()
        if not os.environ.get("HEXIEL_NO_UPDATE_CHECK"):
            threading.Thread(target=self._check_update, daemon=True).start()

    def _apply_sidebar_visibility(self) -> None:
        wanted = self._sidebar_forced
        if wanted is None:
            wanted = self.size.width >= SIDEBAR_MIN_SCREEN
        self.query_one(Sidebar).display = wanted

    def on_resize(self) -> None:
        self._apply_sidebar_visibility()

    def action_toggle_sidebar(self) -> None:
        sidebar = self.query_one(Sidebar)
        self._sidebar_forced = not sidebar.display
        sidebar.display = self._sidebar_forced
        if self._sidebar_forced:
            sidebar.refresh_text()

    def replay_history(self) -> None:
        """Render an existing conversation (e.g. after -c resume) into the chat."""
        chat = self.chat
        # A resumed conversation is no longer empty — drop the welcome card.
        for child in list(chat.children):
            if getattr(child, "id", None) == "welcome" or isinstance(child, Welcome):
                child.remove()
                break
        pending: dict[str, ToolLine] = {}
        for msg in self.session.messages:
            role = msg["role"]
            if role == "user":
                body = msg.get("content", "")
                if body.startswith("<auto-compaction>"):
                    chat.mount(Static("— earlier history auto-compacted —", classes="note"))
                elif strip_env_note(body).strip():
                    self.add_user_msg(strip_env_note(body).removeprefix(STEERING_NOTE)[:2000], stamp(msg.get("ts")))   # env note + steering marker are bookkeeping, not the user's words
            elif role == "assistant":
                # the model's text precedes the tool calls it then makes
                text = msg.get("content", "")
                if text.strip():
                    chat.mount(ResponseHeader(stamp(msg.get("ts"))))
                    chat.mount(Markdown(text, classes="assistant-msg"))
                for call in msg.get("tool_calls", []):
                    wid = ToolLine(classes="tool")
                    pending[call["id"]] = wid
                    wid.running(call_short(call))
                    chat.mount(wid)
            elif role == "graphic":
                card = msg.get("graphics") or {}
                if card.get("kind") == "weather":
                    from .graphics import tui_lines
                    chat.mount(Static("\n".join(tui_lines(card)).replace("[", "\\["),
                                      classes="note weather"))
            elif role == "tool":
                wid = pending.get(msg.get("tool_call_id", ""))
                if wid is not None:
                    call = next((c for m in self.session.messages if m.get("tool_calls", [])
                                 for c in m["tool_calls"] if c["id"] == msg.get("tool_call_id")), {})
                    wid.complete(call_short(call), msg.get("content", ""),
                                 bool(msg.get("is_error")), 0.0)

    # ---------------------------------------------------------------- input

    def on_prompt_input_value_changed(self, event: PromptInput.ValueChanged) -> None:
        text = event.value
        if text.startswith("/") and "\n" not in text:
            profiles = self._cached_profiles() if text.startswith("/model ") else {}
            self.menu.show_for(text, self.skills, profiles,
                               self.session.model_config.name, self._cached_commands())
            return
        self._maybe_path_menu()

    def _maybe_path_menu(self) -> None:
        """Pop the path menu while the caret sits on a completable path token (@, ~, ./, quoted
        paths, anything containing a /); hide it for prose. Suppressed while ↑/↓ browses
        history — recalled entries often end in paths and the menu would steal the keys."""
        from . import complete
        if getattr(self, "_hist_pos", None) is not None:
            self.menu.hide()
            return
        prompt = self.prompt
        token = complete.find_path_token(prompt.text_before_cursor())
        if token is None:
            self.menu.hide()
            return
        cache = getattr(self, "_dir_cache", None)
        if cache is None:                          # one listing cache per app lifetime
            self._dir_cache = complete.cache_keeper()
            cache = self._dir_cache
        comps = complete.completions(token, self.session.cwd, cache)
        if not comps:
            self.menu.hide()
            return
        if self.menu._path_token == token and self.menu.display:
            return                                     # already showing exactly these rows
        self.menu.show_paths(token, comps)

    MENU_CACHE_SECONDS = 3.0

    def _cached_profiles(self) -> dict:
        """Model profiles for the /model menu (config.toml parsed at most every few seconds — the
        menu redraws per keystroke, and parsing the file on each key showed up in typing lag)."""
        at, value = self._profile_cache
        now = time.time()
        if now - at > self.MENU_CACHE_SECONDS:
            from .config import load_config
            try:
                value = load_config().models
            except SystemExit:
                value = {}
            self._profile_cache = (now, value)
        return self._profile_cache[1]

    def _cached_commands(self) -> dict:
        """Custom /commands from the project and home dirs (walked at most every few seconds)."""
        at, value = self._commands_cache
        now = time.time()
        if now - at > self.MENU_CACHE_SECONDS:
            from . import commands as commands_mod
            value = commands_mod.discover(self.session.cwd)
            self._commands_cache = (now, value)
        return self._commands_cache[1]

    @property
    def prompt(self) -> PromptInput:
        if getattr(self, "_prompt", None) is None:
            self._prompt = self.query_one("#prompt", PromptInput)
        return self._prompt

    def accept_menu(self, run: bool) -> bool:
        """Apply the highlighted menu entry. Returns True when it consumed the key."""
        from . import complete
        menu = self.menu
        picked = menu.selected()
        if not picked:
            return False
        kind, value = picked
        prompt = self.prompt
        if kind == "path":                     # complete in place; keep editing (never submit)
            token, entry = menu._path_token, value
            comps = getattr(menu, "_path_comps", None)
            if token and entry and comps:
                finisher = next((c for c in comps if c["name"] == entry), None)
                if finisher:
                    prompt.accept_path(token, complete.finish(token, finisher))
            menu.hide()
            return True
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
        self.menu.hide()
        self.submit_text(event.value)

    def submit_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self._remember(text)
        self._hist_pos = None
        self._hist_draft = ""
        self.chat.anchor()          # sending something means you're following the reply again
        text = self.maybe_command(text)
        if not text:
            return
        self.remove_welcome()
        self.add_user_msg(text)
        self.show_attachments(text)
        self._start_turn(text)

    HISTORY_MAX = 25              # the recall window: the last 25 things you sent this session

    def _remember(self, text: str) -> None:
        """Track what you sent for ↑/↓ recall. Attachments are named in the text (path/@file.png),
        so a recalled entry brings its images and PDFs back with it."""
        if not text or self._history[-1:] == [text]:
            return
        self._history.append(text)
        del self._history[:-self.HISTORY_MAX]

    def remove_welcome(self) -> None:
        chat = self.chat
        for child in list(chat.children):
            if getattr(child, "id", None) == "welcome" or isinstance(child, Welcome):
                child.remove()
                break

    def show_attachments(self, text: str) -> None:
        """Images and PDFs named in a message appear under it, so you can see what you attached."""
        from . import media
        for info in media.attachments_in(text, self.session.cwd):
            self.mount_attachment(info)

    def mount_attachment(self, info: dict) -> None:
        chat = self.chat
        thumb = thumbnail_for(info)
        if thumb is not None:
            chat.mount(thumb)
        chat.mount(AttachmentChip(info))

    def maybe_command(self, text: str) -> str | None:
        if not text.startswith("/"):
            return text
        cmd, _, arg = text[1:].partition(" ")
        arg = arg.strip()
        if cmd in ("delegate", "solo", "cloud"):       # handled by the session itself (it parses the prefix)
            return text
        handler = {
            "help": self.cmd_help, "clear": self.cmd_clear, "compact": self.cmd_compact,
            "fork": self.cmd_fork, "share": self.cmd_share, "watch": self.cmd_watch,
            "wt": self.cmd_wt,
            "config": self.cmd_config, "update": self.cmd_update, "mcp": self.cmd_mcp,
            "prompt": self.cmd_prompt, "context": self.cmd_context,
            "plan": self.cmd_plan, "permissions": self.cmd_permissions,
            "undo": self.cmd_undo, "usage": self.cmd_usage, "spend": self.cmd_spend, "toolmodels": self.cmd_toolmodels, "jobs": self.cmd_jobs, "knowledge": self.cmd_knowledge, "inbox": self.cmd_inbox,
            "model": self.cmd_model, "models": self.cmd_models, "auto": self.cmd_auto,
            "memory": self.cmd_memory, "skills": self.cmd_skills,
            "ship": self.cmd_ship, "heal": self.cmd_heal,
            "remote-control": self.cmd_remote_control,
            "bug": self.cmd_bug, "feature": self.cmd_feature, "bugs": self.cmd_bugs,
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
        self.chat.remove_children()
        self._refresh_status()

    def cmd_fork(self, arg: str) -> None:
        """#27 /fork — branch the conversation here, or at turn N, into a new session file.
        The original chat keeps everything and stays saved under its own id."""
        if self._thread and self._thread.is_alive():
            self.add_note("finish or Ctrl+C the current turn first, then /fork")
            return
        from .session import fork_payload, real_turn_starts
        upto = None
        if arg:
            try:
                upto = max(1, int(arg.split()[0]))
            except ValueError:
                self.add_note("usage: /fork [turn] — branch this conversation from here, "
                              "or keep only turns 1..N in the branch")
                return
        old = self.session
        data, kept = fork_payload(old, upto)
        all_turns = len(real_turn_starts(old.messages))
        if not data.get("messages") or kept == 0:
            self.add_note("nothing to branch yet — no completed turn to fork from")
            return
        from .main import build_session
        try:
            forked = build_session(old.cwd, None, data, reuse=old)
        except Exception as exc:  # noqa: BLE001 - a bad profile must not ping the (un)forking UI
            self.add_note(f"could not start the forked session: {exc!r}")
            return
        old.save()
        forked.save()
        self.session = forked
        self.chat.remove_children()
        self.replay_history()
        self.chat.anchor()
        if upto is not None and kept < all_turns:
            self.add_note(f"🌿 branched — turns 1..{kept} of {all_turns} into {forked.id}.json "
                          f"(the original chat file keeps all {all_turns}; this tab now lives in the branch)")
        else:
            self.add_note(f"🌿 branched — {len(forked.messages)} messages split off into {forked.id}.json; "
                          "the original chat file is untouched and this tab now lives in the branch")
        self._refresh_status()
        return

    def cmd_share(self, arg: str) -> None:
        """#28 /share — a self-contained single-file HTML transcript, secrets scrubbed,
        placed next to the session's history file; open or attach it anywhere."""
        if not self.session.messages:
            self.add_note("nothing to share yet — this conversation is empty")
            return
        from . import share
        try:
            path = share.export_html(self.session, Path(arg) if arg else None)
        except Exception as exc:  # noqa: BLE001 - a bad path/arg must not break the chat
            self.add_note(f"could not write the transcript: {exc!r}")
            return
        from .config import home_data_dir
        where = str(path)
        home = str(Path.home())
        if where.startswith(home + "/"):
            where = "~" + where[len(home) + 1:]
        self.add_note(f"📄 transcript shared → {where}  (self-contained HTML — "
                      "open it in a browser or attach it anywhere)")

    def cmd_watch(self, arg: str) -> None:
        """#29 /watch — react to AI:/AI? comments as you save files in any editor.
        /watch starts, /watch off (or /watch again) stops; the path it scans defaults
        to this project."""
        from . import watch as watch_mod
        arg = arg.strip()
        if self._watch is not None and (not arg or arg.lower() in ("off", "stop")):
            self._watch.stop()
            self._watch = None
            self.add_note("👁 watch off — AI:/AI? comments are just comments again")
            return
        if self._watch is not None:
            self.add_note("already watching — /watch off first to change what it scans")
            return
        root = arg or self.session.cwd
        if not Path(root).is_dir():
            self.add_note(f"no such directory: {root}")
            return
        self._watch = watch_mod.Watcher(root, lambda p, n, k, t:
                                        self._post(self._watch_submit, self._watch, p, n, k, t))
        self._watch.start_background()
        self.add_note(f"👁 watching {root} — write 'AI: do this' or 'AI? question' in any "
                      f"file and save; hexiel acts on the save within {watch_mod.TICK:.0f}s. "
                      "Stop with /watch off.")

    def _watch_submit(self, watcher, path, line_no, kind, text) -> None:
        """Runs on the UI thread. A comment fires only if this is still the active
        watcher — a stop/start between the tick and the callback never submits a
        stale comment into a session that moved on (or was forked)."""
        if watcher is not self._watch:
            return
        label = "AI? question" if kind == "?" else "AI: instruction"
        self.add_note(f"[watch · {label}] {path.name}:{line_no} — {text}")
        self._start_turn(watcher.prompt_for(path, line_no, kind, text))

    def cmd_wt(self, arg: str) -> None:
        """#36 /wt — git worktree isolation: list, create, open (this tab moves into it),
        remove, prune. Sub+headless runs can use `worktree` / --worktree on their own."""
        from . import worktree as wt_mod
        sub, _, rest = arg.strip().partition(" ")
        sub, rest = sub.lower(), rest.strip()
        if not wt_mod.is_repo(self.session.cwd):
            self.add_note(f"/wt needs a git repository — this chat runs in {self.session.cwd}")
            return
        if sub in ("", "list"):
            lines = ["worktrees:"]
            for w in wt_mod.list_worktrees(self.session.cwd):
                mark = "①" if w.is_main else "·"
                lines.append(f" {mark} {w.path}  [{w.branch or '(detached)'}]")
            self.add_note("\n".join(lines) + "\ncreate with /wt create <name> ; open with /wt open <name>")
            return
        if self._thread and self._thread.is_alive() and sub in ("create", "open", "remove", "prune"):
            self.add_note("finish or Ctrl+C the current turn first, then /wt " + sub)
            return
        if sub == "create":
            name, _, branch = rest.partition(" ")
            try:
                w = wt_mod.create(self.session.cwd, name, branch=branch.strip(),
                                  hooks=self.session.hooks, session_id=self.session.id,
                                  on_note=self.add_note)
            except (ValueError, subprocess.CalledProcessError) as exc:
                self.add_note(str(getattr(exc, "stderr", "") or exc).strip()[:240])
                return
            self.add_note(f"🌿 worktree {w.path} ready on branch {w.branch or '(detached)'} — "
                          f"open it with /wt open {w.path.name}; merge after review: "
                          f"`git merge {w.branch}`")
            return
        if sub == "open":
            if not rest:
                self.add_note("usage: /wt open <name> — /wt lists the names")
                return
            try:
                new_cwd = wt_mod.open_session_cwd_for(self.session.cwd, rest)
            except ValueError as exc:
                self.add_note(str(exc))
                return
            from .main import build_session
            try:
                opened = build_session(new_cwd, None, reuse=self.session)
            except Exception as exc:  # noqa: BLE001 - a bad profile must not ping the (un)opening UI
                self.add_note(f"could not start a session in the worktree: {exc!r}")
                return
            self.session.save()
            self.session = opened
            self.session.save()
            self.chat.remove_children()
            self.replay_history()
            self.chat.anchor()
            self.add_note(f"🌿 now in worktree {new_cwd} — changes here never touch the main checkout "
                          "(merge when reviewed: `git merge <branch>`)")
            self._refresh_status()
            return
        if sub == "remove":
            if not rest:
                self.add_note("usage: /wt remove <name>")
                return
            try:
                self.add_note(wt_mod.remove(self.session.cwd, rest))
            except (ValueError, subprocess.CalledProcessError) as exc:
                self.add_note(str(getattr(exc, "stderr", "") or exc).strip()[:240])
            return
        if sub == "prune":
            self.add_note(wt_mod.prune(self.session.cwd))
            return
        self.add_note("usage: /wt [list] | create <name> [branch] | open <name> | remove <name> | prune")

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
                context_window=base.context_window, max_output_tokens=base.max_output_tokens,
                compact_threshold=base.compact_threshold)
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
            self._post(
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
                self._post(self.add_note, f"hexiel {info['current']} is the newest release "
                           f"(latest on GitLab: {info['latest'] or 'unknown'})")
                return
            self._post(self.add_note, f"updating {info['current']} → {info['latest']}…")
            ok, log = updater.apply_update()
            self._post(self.add_note, log.replace("[", "\\["))
            if ok:
                self._post(self._offer_restart, info["latest"])
        self.activity.start()
        threading.Thread(target=lambda: (work(), self._post(self.activity.stop)),
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
        chat = self.chat
        for f in files:
            chat.mount(Markdown(f.read_text()[:4000]))

    def cmd_undo(self, arg: str) -> None:
        if self._thread and self._thread.is_alive():
            self.add_note("finish or Ctrl+C the current turn first, then /undo")
            return
        restored = self.session.undo()
        if not restored:
            self.add_note("nothing to undo — no checkpoint has file changes")
            return
        self.add_note("↶ undid the last turn's file changes:\n" + "\n".join(restored)
                      + "\n(when the repo is git, this also covers shell-command changes: the durable turn checkpoints)")

    def cmd_remote_control(self, arg: str) -> None:
        """/remote-control [on|off|status] — drive this same chat from remote.hexiel.tech."""
        from . import remote_control as rc
        word = arg.lower()
        if word in ("off", "stop"):
            threading.Thread(target=lambda: self._post(self.add_note, rc.stop(self.session)),
                             daemon=True).start()
        elif word == "status" or rc.active(self.session):
            self.add_note(rc.status_text(self.session))
        elif not rc.signed_in():
            self.add_note("Hexiel Remote needs an account first — sign in below (or choose Sign up).")
            self.push_screen(RemoteLoginScreen(), callback=self._remote_login_done)
        else:
            self._remote_on()

    def _remote_login_done(self, creds) -> None:
        if not creds:
            self.add_note("remote control not started")
            return
        from . import remote_control as rc

        def work() -> None:
            try:
                rc.sign_in(*creds)
            except (RuntimeError, OSError) as exc:
                self._post(self.add_note, f"sign-in failed: {exc}")
                return
            self._post(self._remote_on)
        self.add_note("signing in…")
        threading.Thread(target=work, daemon=True).start()

    def _remote_on(self) -> None:
        from . import remote_control as rc
        self.add_note("connecting to Hexiel Remote…")
        say = lambda text: self._post(self.add_note, "remote: " + text)      # noqa: E731
        threading.Thread(target=lambda: self._post(
            self.add_note, rc.start(self.session, driver=_RemoteDriver(self), say=say)), daemon=True).start()

    def _dismiss_approval(self) -> None:
        """A browser answered the approval first: close the terminal's prompt."""
        def close() -> None:
            if isinstance(self.screen, ApproveScreen):
                self.screen.dismiss("no")
        self._post(close)

    def _post(self, fn, *args) -> None:
        """Run fn (with args) on the UI thread without waiting. Every worker→UI path goes through
        this: the old call_from_thread calls blocked the agent thread on drawing, and once the
        window was closing they raised out of the worker threads — tracebacks on close. Scheduled
        in order and dropped quietly at shutdown instead."""
        loop = getattr(self, "_loop", None)
        if loop is None or loop.is_closed():
            return

        async def run() -> None:
            with self._context():             # as call_from_thread does: widgets need the active app
                try:
                    fn(*args)
                except Exception as exc:  # noqa: BLE001 - a widget bug must not kill the feed
                    if getattr(self, "_exit", False):
                        return                # the window is closing: widgets are being torn down
                    try:
                        self.add_note(f"ui error: {exc!r}")
                    except Exception:  # noqa: BLE001
                        pass
        try:
            asyncio.run_coroutine_threadsafe(run(), loop)
        except RuntimeError:                  # closed in the meantime
            pass

    def on_unmount(self) -> None:
        # Quitting stops whatever is still running: a plan step waiting on an approval nobody can answer now
        # kept Python from exiting (it waits for those threads) until Ctrl+C.
        self.session.stop()
        if self._heal:
            self._heal["original"].stop()

    def cmd_bug(self, arg: str, kind: str = "bug") -> None:
        """/bug <what happened> and /feature <idea> — send it to the hexiel team."""
        from . import heal as heal_mod, reporter
        recent = heal_mod.unhealed(self.session.cwd, 3)
        threading.Thread(target=lambda: self._post(
            self.add_note, reporter.file_report(kind, arg, recent)), daemon=True).start()

    def cmd_feature(self, arg: str) -> None:
        self.cmd_bug(arg, "feature")

    def cmd_bugs(self, arg: str) -> None:
        from . import reporter
        threading.Thread(target=lambda: self._post(
            self.add_note, reporter.my_reports_text()), daemon=True).start()

    def cmd_usage(self, arg: str) -> None:
        t = self.session.usage_totals
        fresh = t["prompt_tokens"] - t["cached_tokens"]
        share = 100 * t["cached_tokens"] // max(t["prompt_tokens"], 1)
        self.add_note(f"this session: {t['requests']} request(s) · input {t['prompt_tokens']:,} tokens "
                      f"({t['cached_tokens']:,} from cache = {share}%, {fresh:,} fresh) · "
                      f"output {t['completion_tokens']:,} tokens · model {self.session.model_config.model}")
        from . import digest, spend
        if digest.summary_line(self.session):
            self.add_note(digest.summary_line(self.session))
        for line in spend.summary_lines(self.session.settings):
            self.add_note(line.replace("[", "\\["))

    def cmd_spend(self, arg: str) -> None:
        from . import spend
        self.add_note(spend.command(self.session.settings, arg).replace("[", "\\["))
        self.query_one(Sidebar).refresh_text()

    def cmd_toolmodels(self, arg: str) -> None:
        from . import digest
        probe = arg.strip().lower() == "test"
        if not probe:
            self.add_note(digest.status_text(self.session))
            return
        self.add_note("timing each tool model…")
        threading.Thread(target=lambda: self._post(self.add_note, digest.status_text(self.session, True)),
                         daemon=True).start()

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
                self._post(self.add_note, "\n".join(
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

    def cmd_prompt(self, arg: str) -> None:
        from . import prompt_edit
        info = prompt_edit.get(self.session)

        def done(result) -> None:
            if result is None:
                return
            kind, scope, text = result
            try:
                self.add_note(prompt_edit.save(self.session, text, scope)
                              if kind == "save" else prompt_edit.reset(self.session))
            except ValueError as exc:
                self.add_note(f"⚠ {exc}")
            self._refresh_status()

        self.push_screen(PromptScreen(info["source"], info["text"]), callback=done)

    def cmd_context(self, arg: str) -> None:
        """/context — what occupies the window; `clear` prunes old tool results."""
        mc = self.session.model_config
        share = mc.compact_share(self.session.settings.compact_threshold)
        if arg.split(" ")[0].startswith("clear"):
            try:
                keep = max(1, int(arg.split(" ")[1])) if len(arg.split(" ")) > 1 else 3
            except ValueError:
                keep = 3
            cleared = self.session.clear_old_tool_results(keep)
            if not cleared:
                self.add_note("nothing to clear — the recent tool results stay; older ones would go")
                return
            self.add_note(f"🧹 cleared {cleared} old tool result(s) (recent {keep} rounds stay) "
                          f"— history is shorter; `/context` to re-check")
        b = self.session.context_breakdown()
        window = max(mc.context_window, 1)
        used = self.session.tokens_now()
        rows = [("system prompt", b["system"]), ("tool definitions", b["tools"])]
        rows += [(f"history: {role}", n) for role, n in sorted(b["messages"].items(),
                                                               key=lambda kv: -kv[1])]
        lines = [f"context: ~{used:,} of {window:,} tokens ({used * 100 // window}% of the window; "
                 f"auto-compact at {share:.0%})"]
        for label, n in rows:
            if n:
                lines.append(f"  {label.ljust(28)} ~{n:,}")
        self.add_note("\n".join(lines))

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
        """/auto cycles off -> everything except commits -> everything; or name one: /auto off|commits|all."""
        from . import settings_edit
        from .config import approve_mode
        words = {"off": "off", "commits": "except-commits", "except-commits": "except-commits", "safe": "except-commits",
                 "all": "all", "everything": "all", "on": "all"}
        now = approve_mode(self.session.auto_approve, self.session.guard_commits)
        if arg.strip():
            if arg.strip().lower() not in words:
                self.add_note("usage: /auto  (cycles)  or  /auto off | commits | all")
                return
            new = words[arg.strip().lower()]
        else:
            new = {"off": "except-commits", "except-commits": "all", "all": "off"}[now]
        settings_edit.apply(self.session, "settings.auto_approve", new, "session")
        self.add_note({"off": "auto-approve OFF — hexiel asks before edits and commands",
                       "except-commits": "auto-approve ON, except commits — everything runs without asking, but git/svn "
                                         "commits and pushes still ask first",
                       "all": "⚠ auto-approve EVERYTHING — nothing asks, including commits, pushes and deletes. "
                              "/auto again to turn it off."}[new])
        self._refresh_status()

    def cmd_memory(self, arg: str) -> None:
        body = memory_mod.index_text(self.session.cwd)
        self.chat.mount(Static(body.replace("[", "\\["), classes="note"))

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
        if getattr(self, "_exit", False) or self._loop is None or self._loop.is_closed():
            return          # the window closed before the queued message could run: leave it
        self._start_turn(text)

    def _start_turn(self, prompt: str) -> None:
        if self._thread and self._thread.is_alive():
            self._esc_armed_at = 0.0
            self.session.queue_message(prompt)
            self.add_note("↳ sent — hexiel will read this before its next step (Ctrl+C stops the current work)")
            return
        self._esc_armed_at = 0.0
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
        self._post(self.handle_event, *event)

    # callbacks used by session ------------------------------------------------

    def handle_event(self, event, *payload, **_kw) -> None:
        chat = self.chat
        if event == "thinking":
            # reasoning models can think for minutes; the activity line shows progress
            self._think_chars += len(payload[0])
            self.activity.add_chars(len(payload[0]))
            self.activity.phase("thinking")
            return
        if self._think_chars and event in ("text", "tool_call", "turn_end"):
            chat.mount(Static(f"[{FAINT}]✻[/] [{MUTED}]thought for ~{self._think_chars // 4} tokens[/]",
                              classes="note"))
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
            self._refresh_status()
            return
        if event in ("timers", "tests"):
            self._refresh_status()
            return
        if event == "graphic":
            card = payload[0] if payload else {}
            if card and card.get("kind") == "weather":
                self.finalize_stream(placeholder=False)
                from .graphics import tui_lines
                chat.mount(Static("\n".join(tui_lines(card)).replace("[", "\\["),
                                  classes="note weather"))
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
            attempt, deadline = (payload[2], payload[3]) if len(payload) > 3 else (0, "")
            which = f"attempt {attempt}: " if attempt else ""
            when = f" (at {deadline})" if deadline else ""
            self.activity.countdown(seconds)
            self.add_note(f"⏸ rate limited — {which}hexiel retries on its own in ~{int(seconds // 60)}m"
                          f"{int(seconds % 60):02d}s{when}. ({reason[:120].replace('[', chr(92) + '[')})")
            return
        if event == "model_unreachable":
            seconds, reason = payload[0], payload[1] if len(payload) > 1 else ""
            attempt, deadline = (payload[2], payload[3]) if len(payload) > 3 else (0, "")
            which = f"attempt {attempt}: " if attempt else ""
            when = f" (at {deadline})" if deadline else ""
            self.activity.countdown(seconds)
            self.add_note(f"⚠ model server not responding — {which}retrying in {int(seconds)}s{when}. "
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
            if self._flush_timer is None:
                # coalesce: paint at most ~8 times a second (fewer on long replies), never per delta —
                # a full Markdown re-render behind every token queued up behind your keystrokes
                self._flush_timer = self.set_timer(self._flush_after(), self._flush_stream)
        elif event == "turn_end":
            self.finalize_stream()
            error = payload[0].get("error") if payload and isinstance(payload[0], dict) else None
            if error:
                chat.mount(Static(f"[red]provider error:[/red] {_literal(error)}", classes="note"))
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
        elif event == "tool_result":
            call, text, is_error = payload[0], payload[1], payload[2]
            elapsed = payload[3] if len(payload) > 3 else 0.0
            wid = self._tool_widgets.pop(call["id"], None)
            if wid:
                wid.complete(call_short(call), text, is_error, elapsed)
            if call.get("name") == "view_image" and not is_error:
                from . import media
                path = Path(os.path.expanduser(str(call.get("arguments", {}).get("path", ""))))
                info = media.describe(path if path.is_absolute() else Path(self.session.cwd) / path)
                if info:
                    self.mount_attachment(info)
            self._refresh_status()
        elif event == "compact_start":
            self.finalize_stream()
            chat.mount(Static("… compacting context (summarizing history) …", classes="note"))
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
            chat.mount(Static(f"[green]{_literal(payload[0])}[/green]", classes="note"))

    # the chat scrolls itself: while the view is anchored to the bottom (see ChatScroll) every
    # mounted widget and longer reply keeps the view pinned there with no scroll calls at all,
    # and while you have scrolled up to read, nothing is allowed to yank it back down.

    STREAM_FLUSH_SECONDS = 0.12      # fastest repaint while streaming (see _paint_stream/_flush_after)

    def _flush_stream(self) -> None:
        self._flush_timer = None
        self._paint_stream()

    def _paint_stream(self) -> None:
        """Put the buffered reply into its widget, mounting header + Markdown on the step's first paint.

        While a step's reply grows very long it is repainted through the whole accumulated text
        every flush, and Rich's re-wrap of hundreds of thousands of characters blocks the event
        loop for ~hundreds of ms each time — that was the typing lag. Live paints therefore show
        only the tail once the text passes LIVE_STREAM_CHARS; the full reply renders once when
        the step finishes."""
        display = self._display_text()
        if self._stream_wid is None:
            if not self._stream_text:
                return
            # construct with the text: an update() issued before the mount completes is silently
            # dropped by Textual
            self._stream_header = ResponseHeader(now_stamp())
            self._stream_wid = Markdown(display, classes="assistant-msg")
            self.chat.mount(self._stream_header)
            self.chat.mount(self._stream_wid)
        elif display != self._rendered_stream:
            self._stream_wid.update(display)
        self._rendered_stream = display

    LIVE_STREAM_CHARS = 16_000    # beyond this, live repaints show only the newest part
    LIVE_STREAM_KEEP = 10_000     # ... this much of it (reparse cost stays small every flush)
    LIVE_PAINT_SECONDS = 0.3      # and at a slower cadence: half a screen a frame is plenty

    def _display_text(self) -> str:
        """What the live Markdown shows right now: everything while short, tail + note when long."""
        text = self._stream_text
        if len(text) <= self.LIVE_STREAM_CHARS:
            return text
        return (f"*… ~{len(text):,} characters so far — the full reply appears when this step finishes.*\n\n"
                + text[-self.LIVE_STREAM_KEEP:])

    def _flush_after(self) -> float:
        """Deltas merge at this interval. A long live repaint re-wraps the tail, so beyond the
        live cap the cadence slows down as well."""
        if len(self._stream_text) <= self.LIVE_STREAM_CHARS:
            return self.STREAM_FLUSH_SECONDS
        return self.LIVE_PAINT_SECONDS

    def finalize_stream(self, placeholder: bool = True) -> None:
        if self._flush_timer is not None:
            self._flush_timer.stop()
            self._flush_timer = None
        self._paint_stream()
        header = self._stream_header
        if self._stream_wid is not None:
            wid, text = self._stream_wid, self._stream_text
            if not text:
                if placeholder:
                    text = "*(empty response)*"
                elif header is not None:
                    header.display = False
            elif text != self._rendered_stream or not wid.is_mounted:
                if wid.is_mounted:
                    wid.update(text)
                else:   # still mounting: an immediate update would be dropped
                    self.call_after_refresh(wid.update, text)
            self._stream_wid = None
            self._stream_header = None
            self._stream_text = ""
            self._rendered_stream = ""
        elif header is not None:
            header.display = False
            self._stream_header = None
        self._refresh_status()

    def add_user_msg(self, text: str, ts: str | None = None) -> None:
        self.chat.mount(UserMessage(text, now_stamp() if ts is None else ts))

    def add_note(self, text: str) -> None:
        self.chat.mount(Static(f"[{FAINT}]·[/] {safe_markup(text)}", classes="note"))

    def _tick(self) -> None:
        if self._stream_wid is not None and self._stream_text \
                and self._stream_text != self._rendered_stream:
            self._paint_stream()        # safety net: a delta that arrives between paints
        self._timers_due()
        self._wake_if_finished()
        self._refresh_status()

    def _wake_if_finished(self) -> None:
        """A background test run / task finished while nothing is running: hand its result to the model."""
        if not getattr(self.session.settings, "auto_wake", True):
            return
        if (self._thread and self._thread.is_alive()) or self.session.compacting:
            return
        from . import tools as tools_mod
        from .session import WAKE_PROMPT
        notes = tools_mod.finished_for_wake()
        if notes:
            for note in notes:
                self.add_note(note)
            self._start_turn(WAKE_PROMPT.format(notes="\n".join(notes)))

    def _timers_due(self) -> None:
        """Say so once when a timer runs out (the footer keeps showing it as over until it is stopped)."""
        from . import timers as timers_mod
        done = self.__dict__.setdefault("_timers_rang", set())
        for timer in timers_mod.live(self.session):
            if timer["ends"] <= time.time() and timer["id"] not in done:
                done.add(timer["id"])
                took = timers_mod.fmt_duration(timer["ends"] - timer["started"])
                self.add_note(f"⏱ {timer['label']}: the {took} estimate has passed — nothing was stopped, "
                              "hexiel keeps working until the task is done".replace("[", "\\["))
                self.notify_user("hexiel timer", f"{timer['label']}: the {took} estimate has passed (still running)")

    def on_text_selected(self, event) -> None:
        """Mouse released after highlighting text anywhere in the chat: copy it, no extra keystroke."""
        try:
            text = self.screen.get_selected_text()
        except Exception:                                    # noqa: BLE001 - copying is a convenience
            text = None
        if text and text.strip():
            copy_selection(self, text)
            self.notify(f"copied {len(text)} characters", timeout=1.5)

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
        self.status.refresh_text()               # cached widgets: a DOM walk here ran several
        try:                                     # times a second while a turn was streaming
            self.sidebar.refresh_text()
        except Exception:
            pass

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

    def cmd_knowledge(self, arg: str) -> None:
        """Reference-docs library: status, or `sync` in the background."""
        from . import knowledge
        if arg.strip() != "sync":
            self.add_note(knowledge.status() + "\n(add docs to the folder, then /knowledge sync)")
            return
        self.add_note(f"syncing {knowledge.knowledge_dir()} …")

        def work() -> None:
            lines: list[str] = []
            try:
                stats = knowledge.sync(say=lines.append)
                if knowledge.has_content() and "knowledge" not in self.session.loaded_toolsets:
                    from . import toolsets
                    toolsets.load(self.session, "knowledge")
                lines.append(f"{stats['added']} added, {stats['changed']} changed, {stats['removed']} removed, "
                             f"{stats['embedded']} embedded")
                lines.append(knowledge.status())
            except Exception as exc:  # noqa: BLE001 - report, don't crash the UI
                lines.append(f"sync failed: {exc}")
            self._post(self.add_note, "\n".join(lines[-8:]))
        threading.Thread(target=work, daemon=True).start()

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
                self._post(self.add_note, f"[red]could not prepare the heal workspace:[/] {exc}")
                return
            self._post(self._enter_heal, original, ws, incidents, arg.strip())
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
        healer.guard_commits = original.guard_commits
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
                self._post(self.add_note, f"[red]could not read the fix:[/] {exc}")
                return
            self._post(self._show_review, found)
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
                    self._post(self.add_note, f"[red]sending failed:[/] {exc}")
                    self._heal_pending = heal
                    self._post(self.add_note, "the fix is applied locally; /heal review to retry sending")
                    self._post(self._exit_heal)
                    self._post(self._offer_restart, "", "self-heal fix applied")
                    return
            heal_mod.mark_healed(heal["original"].cwd)
            contribute.discard(heal["ws"])
            self._post(self.add_note, msg)
            self._post(self._exit_heal)
            self._post(self._offer_restart, "", "self-heal fix applied")
        self.activity.start()
        threading.Thread(target=lambda: (work(), self._post(self.activity.stop)),
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
                preview = str(args["text"])[:1800]
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

        self._post(show)
        # wait however long it takes, but Stop (Ctrl+C, or from Hexiel Remote) gives up and closes the prompt
        while not holder["event"].wait(0.5):
            if self.session._stop.is_set():
                self._dismiss_approval()
                return "no"
        return holder["result"]

    def action_cancel(self) -> None:
        if self._thread and self._thread.is_alive():
            self.session.stop()
            self.add_note("canceling turn…")
            return
        if self.prompt.value:
            self.prompt.value = ""
            self._hist_pos = None
            self._hist_draft = ""
            return
        self.exit()

    ESC_CANCEL_SECONDS = 1.5

    def escape_pressed(self) -> None:
        """First Esc asks for confirmation; a second Esc within 1.5s stops the current task
        (Esc Esc) — the cancel key for people (and terminals) where Ctrl+C reads as 'close'."""
        now = time.time()
        if not (self._thread and self._thread.is_alive()):
            self._esc_armed_at = 0.0
            return
        if now - self._esc_armed_at < self.ESC_CANCEL_SECONDS:
            self._esc_armed_at = 0.0
            self.session.stop()
            self.add_note("stopping the current task… (Esc Esc)")
        else:
            self._esc_armed_at = now
            self.notify("press Esc again to stop the current task", timeout=2)

    def action_clear_chat(self) -> None:
        self.cmd_clear("")


def _literal(text) -> str:
    """Text from a model, a tool or a server, shown as-is (square brackets are not formatting)."""
    return str(text).replace("[", "\\[")


def safe_markup(text: str) -> str:
    """Notes may carry simple formatting like [b]…[/b]; if the text isn't valid markup (a traceback, JSON, a
    shell snippet full of [brackets]) show it literally instead of letting the renderer crash the whole UI."""
    try:
        from textual.content import Content
        Content.from_markup(text)
        return text
    except Exception:  # noqa: BLE001 - any parse failure means "treat it as plain text"
        return _literal(text)


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