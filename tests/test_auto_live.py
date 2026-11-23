"""Changing /auto takes effect at once: in the middle of a long task, for sub-agents already running, and for an
approval already waiting for an answer (nothing waits for the current step or turn to finish)."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import settings_edit
from hexiel.config import ModelConfig, Settings, approve_mode
from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, build_tools

aiohttp = pytest.importorskip("aiohttp")
MC = ModelConfig(name="t", provider="openai", model="m", base_url="http://127.0.0.1:9/v1")


def make_session():
    here = str(Path(__file__).parent)
    s = Session(MC, Settings(), cwd=here, tools=build_tools(Toolchain()), system_prompt="t")
    s.bind_tool_ctx(ToolContext(cwd=here, chain=Toolchain()))
    s.save = lambda: None
    return s


def test_a_mode_change_reaches_running_sub_agents():
    import weakref
    parent, helper = make_session(), make_session()
    parent._helpers = weakref.WeakSet([helper])
    settings_edit.apply(parent, "settings.auto_approve", "all", "session")
    assert approve_mode(helper.auto_approve, helper.guard_commits) == "all"
    settings_edit.apply(parent, "settings.auto_approve", "except-commits", "session")
    assert approve_mode(helper.auto_approve, helper.guard_commits) == "except-commits"


async def _mid_turn(monkeypatch, mode, command):
    import hexiel.session as S
    from aiohttp import web
    from hexiel.webui import make_app
    session = make_session()
    verdict, finished = {}, asyncio.Event()
    loop = asyncio.get_running_loop()

    def long_turn(self, user_text):
        self.on_event("text", "working")
        verdict["v"] = self.on_approve("bash", {"command": command})     # waits for an answer, like a real step
        loop.call_soon_threadsafe(finished.set)
        self.on_event("turn_end")
    monkeypatch.setattr(S.Session, "_turn_body", long_turn)
    app = make_app(session)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as client:
            async with client.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
                await asyncio.wait_for(ws.receive_str(), 5)
                await ws.send_str(json.dumps({"t": "send", "text": "do the long thing"}))
                while json.loads(await asyncio.wait_for(ws.receive_str(), 5))["t"] != "approval":
                    pass                                                   # the turn is now stuck on a question
                await ws.send_str(json.dumps({"t": "send", "text": f"/auto {mode}"}))
                await asyncio.wait_for(finished.wait(), 5)                 # answered without anyone tapping Yes
    finally:
        await runner.cleanup()
    return session, verdict


def test_auto_all_mid_turn_answers_the_waiting_approval(monkeypatch):
    session, verdict = asyncio.run(_mid_turn(monkeypatch, "all", "ls"))
    assert approve_mode(session.auto_approve, session.guard_commits) == "all" and verdict["v"] == "yes"


def test_auto_commits_mid_turn_answers_an_ordinary_step(monkeypatch):
    session, verdict = asyncio.run(_mid_turn(monkeypatch, "commits", "npm test"))
    assert approve_mode(session.auto_approve, session.guard_commits) == "except-commits" and verdict["v"] == "yes"


def test_auto_commits_mid_turn_still_asks_about_a_commit(monkeypatch):
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(_mid_turn(monkeypatch, "commits", "git commit -m x"))
