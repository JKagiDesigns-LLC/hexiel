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
