"""Messages typed while hexiel is working must reach the model — before its next step, or as a follow-up —
and never be dropped (they used to print 'still working' and vanish)."""

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel.config import ModelConfig, Settings
from hexiel.session import STEERING_NOTE, Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, ToolDef, text_result

MC = ModelConfig(name="t", provider="openai", model="m", base_url="http://x")


def make(tmp_path, monkeypatch, replies, on_tool=None):
    tools = [ToolDef("noop", "does nothing", {"type": "object", "properties": {}}, readonly=True,
                     run=lambda args, ctx: (on_tool() if on_tool else None) or text_result("done"))]
    s = Session(MC, Settings(), str(tmp_path), tools, "sys")
    s.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    s.save = lambda: None
    events, seen = [], []
    s.on_event = lambda e, *p, **k: events.append((e, *p))
    queue = list(replies)

    def fake_once(self, provider, defs):
        seen.append([dict(m) for m in self.messages])          # what the model is shown on this request
        reply = queue.pop(0)
        if callable(reply):
            reply = reply()
        return reply
    monkeypatch.setattr(Session, "_stream_once", fake_once)
    monkeypatch.setattr("hexiel.llm.make_provider", lambda *a, **k: object())
    return s, events, seen


CALL = {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "noop", "arguments": {}}]}


def test_a_message_typed_during_a_tool_call_is_read_before_the_next_request(tmp_path, monkeypatch):
    holder = {}
    s, events, seen = make(tmp_path, monkeypatch, [CALL, {"role": "assistant", "content": "ok, listening"}],
                           on_tool=lambda: holder["s"].queue_message("NO it is on macmini, stop arguing"))
    holder["s"] = s
    s.run_turn("find jellyfin")
    assert len(seen) == 2
    second = seen[1]
    assert second[-1]["role"] == "tool" or any(
        m["role"] == "user" and "stop arguing" in m["content"] for m in second)
    delivered = [m for m in second if m["role"] == "user" and "stop arguing" in m["content"]]
    assert delivered and delivered[0]["content"].startswith(STEERING_NOTE)
    assert [m["role"] for m in second][-2:] == ["tool", "user"]           # right after the tool result
    assert any(e[0] == "note" and "read your new message" in e[1] for e in events)
    assert events[-1][0] == "turn_end" and sum(1 for e in events if e[0] == "turn_end") == 1


def test_a_message_typed_while_the_final_reply_streams_gets_its_own_answer(tmp_path, monkeypatch):
    holder = {}

    def first_reply():
        holder["s"].queue_message("also, use python 3.12")           # arrives while the model is replying
        return {"role": "assistant", "content": "all done"}
    s, events, seen = make(tmp_path, monkeypatch, [first_reply, {"role": "assistant", "content": "switched to 3.12"}])
    holder["s"] = s
    s.run_turn("set up the project")
    assert len(seen) == 2 and "python 3.12" in seen[1][-1]["content"]
    assert s.messages[-1]["content"] == "switched to 3.12" and not s.has_pending()


def test_several_messages_are_all_delivered_in_order(tmp_path, monkeypatch):
    s, events, seen = make(tmp_path, monkeypatch, [{"role": "assistant", "content": "ok"}])
    s.queue_message("first")
    s.queue_message("   ")                                              # blank messages are ignored
    s.queue_message("second")
    s.run_turn("go")
    users = [m["content"] for m in seen[0] if m["role"] == "user"]
    assert users[0].endswith("go") and users[1].endswith("first") and users[2].endswith("second")   # each turn opens with the env note
    assert any(e[0] == "note" and "2 new messages" in e[1] for e in events)


def test_a_cancelled_turn_keeps_queued_messages_for_a_follow_up(tmp_path, monkeypatch):
    holder = {}
    def interrupt():
        holder["s"].queue_message("change of plans")
        holder["s"].stop()
    s, events, seen = make(tmp_path, monkeypatch, [CALL], on_tool=interrupt)
    holder["s"] = s
    s.run_turn("do the long thing")
    assert events[-1][0] == "turn_end"
    assert s.take_pending() == ["change of plans"]                        # not lost: the UI starts a follow-up turn


# ---------------------------------------------------------------- /delegate

def test_a_delegated_plan_pauses_when_the_user_speaks(tmp_path, monkeypatch):
    from test_delegate import PLANNER, WORKER, Planner, Worker, plan_json
    from hexiel import llm, planner
    providers = {"cloud": Planner(plan_json("a.txt", "b.txt", "c.txt")), "local": Worker()}
    monkeypatch.setattr(llm, "make_provider", lambda mc, settings: providers[mc.name])
    monkeypatch.setattr(planner, "_config_profile", lambda session, name: {"cloud": PLANNER, "local": WORKER}.get(name))
    from test_delegate import _write_tool
    tools = [ToolDef("write", "w", {"type": "object", "properties": {}}, run=_write_tool)]
    s = Session(WORKER, Settings(planner_profile="cloud", executor_profile="local"), str(tmp_path), tools, "sys")
    s.auto_approve = True
    s.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    s.save = lambda: None
    s.on_approve = lambda *a: "yes"
    events = []

    def watch(event, *p, **k):
        events.append((event, *p))
        if event == "note" and p[0].startswith("✓ step 1/"):
            s.queue_message("wait, use b.md instead")
    s.on_event = watch
    s.run_turn("/delegate make three files")
    assert (tmp_path / "a.txt").exists() and not (tmp_path / "b.txt").exists()
    assert any("pausing the plan" in e[1] for e in events if e[0] == "note")
    assert "Plan stopped: 1/3" in s.messages[-1]["content"]
    assert s.take_pending() == ["wait, use b.md instead"]


# ---------------------------------------------------------------- terminal UI

def test_tui_queues_a_message_typed_mid_turn_and_runs_leftovers_as_a_follow_up(tmp_path):
    from hexiel.tui import HexielTUI

    async def go():
        s = Session(MC, Settings(), str(tmp_path), [], "sys")
        s.save = lambda: None
        prompts, release = [], threading.Event()

        def fake_run_turn(text):
            prompts.append(text)
            if len(prompts) == 1:
                release.wait(5)                          # the first turn is "busy" until released
        s.run_turn = fake_run_turn
        app = HexielTUI(s, [], set())
        async with app.run_test(size=(100, 30)) as pilot:
            app._start_turn("first")
            await pilot.pause(0.3)
            app._start_turn("NOOO it is on macmini")      # typed while the first turn is running
            await pilot.pause(0.2)
            assert s.has_pending() and prompts == ["first"]          # queued, not dropped, not a second thread
            release.set()
            for _ in range(40):
                await pilot.pause(0.1)
                if len(prompts) == 2:
                    break
            return prompts, s.has_pending()
    prompts, pending = asyncio.run(go())
    assert prompts == ["first", "NOOO it is on macmini"] and not pending


# ---------------------------------------------------------------- browser UI

aiohttp = pytest.importorskip("aiohttp")


def test_web_ui_queues_a_message_sent_while_busy(tmp_path, monkeypatch):
    from hexiel.webui import make_app
    import hexiel.session as S
    release, seen = threading.Event(), []

    def slow_turn(self, user_text):
        seen.append(user_text)
        release.wait(5)
        self.on_event("turn_end")
    monkeypatch.setattr(S.Session, "_turn_body", slow_turn)
    s = Session(MC, Settings(), str(tmp_path), [], "sys")
    s.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    s.save = lambda: None

    async def go():
        from aiohttp import web
        runner = web.AppRunner(make_app(s))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        notes = []
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
                    await asyncio.wait_for(ws.receive_str(), 5)
                    await ws.send_str(json.dumps({"t": "send", "text": "first"}))
                    await asyncio.sleep(0.4)
                    await ws.send_str(json.dumps({"t": "send", "text": "actually, use macmini"}))
                    for _ in range(20):
                        data = json.loads(await asyncio.wait_for(ws.receive_str(), 5))
                        if data["t"] == "note":
                            notes.append(data["text"])
                            break
                    assert s.has_pending() and seen == ["first"]
                    release.set()
                    for _ in range(40):
                        await asyncio.sleep(0.1)
                        if len(seen) == 2:
                            break
        finally:
            release.set()
            await runner.cleanup()
        return notes
    notes = asyncio.run(go())
    assert seen == ["first", "actually, use macmini"] and notes and "sent" in notes[0]
