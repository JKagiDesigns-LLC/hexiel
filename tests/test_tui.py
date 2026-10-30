"""TUI behaviour: chronological turn rendering, slash menu, activity line, config screen."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from textual.widgets import Markdown

from hexiel.config import ModelConfig, Settings
from hexiel.session import Session
from hexiel.tui import ActivityLine, CommandMenu, HexielTUI, ToolLine


def make_app(tmp_path) -> HexielTUI:
    mc = ModelConfig(name="t", provider="openai", model="m", base_url="")
    return HexielTUI(Session(mc, Settings(), str(tmp_path), [], "sys"), [], set())


def run(coro):
    return asyncio.run(coro)


def test_turn_renders_in_chronological_order(tmp_path):
    async def go():
        app = make_app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            call = {"id": "c1", "name": "grep", "arguments": {"pattern": "x"}}
            app.handle_event("text", "Let me look first.")
            app.handle_event("tool_call", call)
            app.handle_event("tool_result", call, "hit.py:1", False, 0.1)
            app.handle_event("text", "Found it in hit.py.")
            app.handle_event("turn_end")
            await pilot.pause()
            chat = app.query_one("#chat")
            order = [type(w).__name__ for w in chat.children if isinstance(w, (Markdown, ToolLine))]
            texts = [w.source for w in chat.children if isinstance(w, Markdown)]
            return order, texts
    order, texts = run(go())
    assert order == ["Markdown", "ToolLine", "Markdown"]     # reply continues *below* the tool
    assert texts == ["Let me look first.", "Found it in hit.py."]


def test_slash_menu_filters_and_completes(tmp_path):
    async def go():
        app = make_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("/", "c", "o")
            await pilot.pause()
            items = [v for _, v in app.query_one(CommandMenu)._items]
            await pilot.press("tab")
            await pilot.pause()
            return items, app.query_one("#prompt").value
    items, value = run(go())
    assert items[:2] == ["config", "compact"] or set(items[:2]) == {"config", "compact"}
    assert value.startswith("/co")


def test_activity_line_lifecycle(tmp_path):
    async def go():
        app = make_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            act = app.query_one(ActivityLine)
            hidden_at_start = not act.display
            act.start()
            act.phase("tool", "test")
            await pilot.pause(0.2)
            shown = str(act.render())
            app.handle_event("turn_end")
            await pilot.pause()
            return hidden_at_start, shown, act.display
    hidden_at_start, shown, still_shown = run(go())
    assert hidden_at_start and "Testing" in shown and "ctrl+c" in shown
    assert not still_shown


def test_replayed_history_keeps_text_before_its_tool_calls(tmp_path):
    async def go():
        app = make_app(tmp_path)
        app.session.messages = [
            {"role": "user", "content": "fix it"},
            {"role": "assistant", "content": "Checking first.",
             "tool_calls": [{"id": "c1", "name": "grep", "arguments": {"pattern": "x"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "hit"},
            {"role": "assistant", "content": "Done."},
        ]
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            return [type(w).__name__ for w in app.query_one("#chat").children
                    if isinstance(w, (Markdown, ToolLine))]
    assert run(go()) == ["Markdown", "ToolLine", "Markdown"]


def test_web_history_order(tmp_path):
    from hexiel.webui import WebState
    app = make_app(tmp_path)
    app.session.messages = [
        {"role": "assistant", "content": "Checking first.",
         "tool_calls": [{"id": "c1", "name": "grep", "arguments": {"pattern": "x"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "hit"},
    ]
    assert [i["t"] for i in WebState(app.session).render_history()] == ["assistant", "tool"]


def test_model_menu_offers_presets(tmp_path):
    async def go():
        app = make_app(tmp_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press(*"/model add son")
            await pilot.pause()
            return [(k, v) for k, v in app.query_one(CommandMenu)._items]
    items = run(go())
    assert ("preset", "sonnet") in items


def test_model_picker_lists_live_results(tmp_path, monkeypatch):
    from hexiel import model_catalog as M
    from hexiel.tui import ModelPicker
    ep_a = M.Endpoint("Ollama @ box", "openai", "http://box:11434/v1")
    ep_b = M.Endpoint("Anthropic", "anthropic", "https://api.anthropic.com")

    def fake_discover(cfg, on_result):
        on_result(ep_b, [M.Found(ep_b, "claude-sonnet-5-5"), M.Found(ep_b, "claude-opus-5-5")], "")
        on_result(ep_a, [M.Found(ep_a, "qwen3.6:35b-a3b-coding", profile="t")], "")
        on_result(M.Endpoint("Groq", "openai", "https://api.groq.com/openai/v1"), [], "HTTP 401")
    monkeypatch.setattr(M, "discover", fake_discover)
    monkeypatch.setattr(M, "endpoints", lambda cfg: [1, 2, 3])

    async def go():
        app = make_app(tmp_path)
        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.press("ctrl+p")
            await pilot.pause(0.3)
            pk = app.screen
            first_group = [r.model for r in pk._pk_rows if r is not None][0]
            await pilot.press(*"opus")
            await pilot.pause(0.2)
            filtered = [r.model for r in pk._pk_rows if r is not None]
            status = str(pk.query_one("#picker-status").render())
            return isinstance(pk, ModelPicker), first_group, filtered, status
    is_picker, first, filtered, status = run(go())
    assert is_picker and first == "qwen3.6:35b-a3b-coding"        # current model's group first
    assert filtered == ["claude-opus-5-5"]
    assert "3 model(s)" in status and "Groq" in status and "401" in status


def test_prompt_accepts_multiline_and_enter_submits(tmp_path):
    async def go():
        app = make_app(tmp_path)
        sent = []
        app.submit_text = sent.append
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("a", "shift+enter", "b", "ctrl+j", "c")
            await pilot.pause()
            typed = app.query_one("#prompt").value
            height = app.query_one("#prompt").outer_size.height
            await pilot.press("enter")
            await pilot.pause()
            return typed, height, sent, app.query_one("#prompt").value
    typed, height, sent, after = run(go())
    assert typed == "a\nb\nc" and height >= 5
    assert sent == ["a\nb\nc"] and after == ""


def test_paste_is_sanitized(tmp_path):
    from textual import events
    from hexiel.tui import sanitize_paste
    assert sanitize_paste("a\r\nb\rc") == "a\nb\nc"
    assert sanitize_paste("\x1b[31mred\x1b[0m \x1b]0;title\x07ok\x00\x07") == "red ok"
    assert sanitize_paste("x‮y​z") == "xyz"
    assert sanitize_paste("a\tb") == "a   b"

    async def go():
        app = make_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            app.query_one("#prompt").focus()
            app.query_one("#prompt").post_message(events.Paste("one\r\n\x1b[1mtwo\x1b[0m\x00"))
            await pilot.pause()
            return app.query_one("#prompt").value
    assert run(go()) == "one\ntwo"


def test_clipboard_save_image_roundtrip(tmp_path):
    from hexiel.clipboard import save_image
    from hexiel.tools import attach_images
    from tests.test_images import PNG
    path = save_image(PNG, "image/png", str(tmp_path))
    images, _ = attach_images(f"look at {path}", str(tmp_path))
    assert len(images) == 1


def test_stopped_turn_does_not_crash_the_agent_thread():
    """Session sends turn_end with aborted=True; the TUI's hook must pass the keyword through."""
    from types import SimpleNamespace
    from hexiel import tui
    seen, notes = [], []

    class FakeSession:
        def run_turn(self, prompt):
            self.on_event("turn_end", aborted=True)

        def save(self):
            pass

        def take_pending(self):
            return []

    app = SimpleNamespace(call_from_thread=lambda fn, *a: fn(*a))
    app_ref = SimpleNamespace(session=FakeSession(), app=app,
                              handle_event=lambda event, *p, **kw: seen.append((event, p, kw)),
                              add_note=notes.append, ask_approval=None,
                              activity=SimpleNamespace(stop=lambda: None))
    tui.TurnThread(app_ref, "hi").run()
    assert seen == [("turn_end", (), {"aborted": True})] and not notes   # no "agent crashed" note


def test_note_ends_the_streaming_block_so_text_after_it_appears_below():
    """/delegate emits notes between steps; reply text after a note must start a new block under it."""
    async def go():
        app = make_app(Path("/tmp"))
        async with app.run_test(size=(100, 40)) as pilot:
            app.handle_event("text", "step one report")
            app.handle_event("note", "✓ step 1/2 done")
            app.handle_event("text", "step two report")
            app.handle_event("turn_end")
            await pilot.pause()
            chat = app.query_one("#chat")
            return [(type(w).__name__, getattr(w, "source", None)) for w in chat.children
                    if isinstance(w, Markdown) or ("note" in w.classes and type(w).__name__ == "Static")]
    seen = run(go())
    assert [kind for kind, _ in seen] == ["Markdown", "Static", "Markdown"]
    assert seen[0][1] == "step one report" and seen[2][1] == "step two report"


def test_status_line_shows_the_model_that_is_working_now(tmp_path):
    async def go():
        app = make_app(tmp_path)
        async with app.run_test(size=(140, 30)) as pilot:
            app.session.on_event = lambda event, *p, **k: app.handle_event(event, *p, **k)   # as TurnThread wires it
            app.session.set_active_model(app.session.model_config, "planning")
            await pilot.pause()
            during = str(app.sidebar._body.content)      # the sidebar's MODEL block names the working model
            app.session.active_model = ""
            app.handle_event("model_active")
            await pilot.pause()
            return during, str(app.sidebar._body.content)
    during, after = run(go())
    assert "m · planning" in during and "planning" not in after


# ---------------------------------------------------------------- square brackets in text must never crash the UI

NASTY = ('python3 -c "import json; streams = [s for s in data.get(\'streams\', []) if s.get(\'codec_type\') == \'video\']; '
         'cmd = [\'ffprobe\', \'-of\', \'json\', str(f)]; x = d[0]" [/b] [bold')


def test_approval_dialog_shows_code_with_square_brackets_literally(tmp_path):
    """Regression: approving a command containing Python like ['ffprobe', '-of', ...] raised MarkupError and
    took the whole terminal UI down."""
    import json
    from hexiel.tui import ApproveScreen

    async def go():
        app = make_app(tmp_path)
        async with app.run_test(size=(110, 40)) as pilot:
            app.push_screen(ApproveScreen("bash", json.dumps({"command": NASTY}, indent=2), "Bash(python3:*)"))
            await pilot.pause(0.4)
            text = str(app.screen.query_one("#approve-args").content)
            app.screen.action_no()
            await pilot.pause(0.2)
            return text
    shown = run(go())
    assert "ffprobe" in shown and "streams = [s for s in" in shown and "[/b]" in shown


def test_plan_approval_text_with_brackets_is_literal(tmp_path):
    from hexiel.tui import ApproveScreen

    async def go():
        app = make_app(tmp_path)
        async with app.run_test(size=(110, 40)) as pilot:
            app.push_screen(ApproveScreen("delegate-plan", "1. fix [x]\n   check: grep -q '[a-z]' f", "for this session"))
            await pilot.pause(0.4)
            return str(app.screen.query_one("#approve-args").content)
    assert "fix [x]" in run(go()) and "\\[" not in run(go())


def test_notes_and_errors_with_brackets_do_not_crash(tmp_path):
    async def go():
        app = make_app(tmp_path)
        async with app.run_test(size=(110, 40)) as pilot:
            app.add_note(NASTY)                                   # not valid markup: shown literally
            app.add_note("[b]bold[/b] still works")               # valid markup stays formatted
            app.handle_event("turn_end", {"error": "provider said: " + NASTY})
            app.handle_event("ship_done", "merged " + NASTY)
            await pilot.pause(0.5)
            return len(list(app.query_one("#chat").children))
    assert run(go()) >= 4                                         # mounted and rendered without raising


def test_safe_markup_keeps_valid_markup_and_escapes_the_rest():
    from hexiel.tui import safe_markup
    assert safe_markup("[b]ok[/b] and [green]fine[/green]") == "[b]ok[/b] and [green]fine[/green]"
    assert safe_markup(NASTY).startswith("python3 -c") and "\\[s for s" in safe_markup(NASTY)


def test_footer_counts_down_a_timer_and_says_when_it_is_up(tmp_path):
    import time
    from hexiel.tui import StatusLine

    async def go():
        app = make_app(tmp_path)
        async with app.run_test(size=(120, 30)) as pilot:
            app.session.timers = [{"id": "a", "label": "test suite", "started": time.time(), "ends": time.time() + 1800}]
            app.handle_event("timers", app.session.timers)
            await pilot.pause()
            footer = str(app.query_one(StatusLine).render())
            app.session.timers[0]["ends"] = time.time() - 5
            app._tick()
            app._tick()                                   # says so once, not every second
            await pilot.pause()
            notes = [str(w.render()) for w in app.query(".note")]
            return footer, notes
    footer, notes = run(go())
    assert "⏱ test suite · 30:00 left" in footer or "⏱ test suite · 29:59 left" in footer
    assert sum("time's up: test suite" in n for n in notes) == 1


def test_quitting_ends_a_step_still_waiting_for_an_approval(tmp_path):
    # a plan step blocked on the terminal's prompt used to keep Python from exiting until Ctrl+C
    import threading
    app = make_app(tmp_path)
    got = []

    async def go():
        async with app.run_test(size=(100, 30)) as pilot:
            t = threading.Thread(target=lambda: got.append(app.ask_approval("bash", {"command": "mkdir docs"})),
                                 daemon=True)
            t.start()
            await pilot.pause(0.5)
            app.exit()
        return t
    t = asyncio.run(go())
    t.join(5)
    assert not t.is_alive() and got == ["no"]
