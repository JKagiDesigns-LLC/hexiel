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
    assert hidden_at_start and "Testing the spirits" in shown and "ctrl+c" in shown
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
