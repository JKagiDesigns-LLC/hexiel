"""Regression test: browser WS roundtrip — hello → send → text deltas → turn_end.

Guards against the two bugs found in September 2026 debugging:
1. handle_ws_message called with a stale argument list (TypeErrors silently
   swallowed per-frame, so 'send' did nothing at all);
2. WebState.on_event had no turn_end branch, so no tab ever learned a turn
   had finished."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel.config import load_config
from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, build_tools

aiohttp = pytest.importorskip("aiohttp")


@pytest.fixture()
def ws_app(monkeypatch):
    from hexiel.config import ModelConfig, Settings
    tools = build_tools(Toolchain())
    ctx = ToolContext(cwd=str(Path(__file__).parent), chain=Toolchain())
    session = Session(ModelConfig(name="t", provider="openai", model="m", base_url="http://127.0.0.1:9/v1"),
                      Settings(), cwd=str(Path(__file__).parent), tools=tools, system_prompt="t")
    session.bind_tool_ctx(ctx)

    # canned turn: stream text deltas from a worker thread, then end the turn
    import hexiel.session as S

    def canned_turn(self, user_text):
        self.on_event("text", "WS-OK")
        self.on_event("turn_end")
    monkeypatch.setattr(S.Session, "_turn_body", canned_turn)

    from hexiel.webui import make_app
    app = make_app(session)
    return app, session


async def _roundtrip(ws_app, aiohttp):
    app, session = ws_app
    from aiohttp import web
    runner = web.AppRunner(app)
    await runner.setup()
    from aiohttp import web as _web
    site = _web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as client:
            async with client.ws_connect(
                    f"http://127.0.0.1:{port}/ws",
                    timeout=aiohttp.ClientTimeout(total=5)) as ws:
                hello = json.loads(await asyncio.wait_for(ws.receive_str(), 5))
                assert hello["t"] == "history"
                await ws.send_str(json.dumps({"t": "send", "text": "hi"}))
                seen_text, seen_end = "", False
                while not seen_end:
                    data = json.loads(await asyncio.wait_for(ws.receive_str(), 5))
                    if data["t"] == "text":
                        seen_text += data["delta"]
                    elif data["t"] == "turn_end":
                        seen_end = True
                assert seen_text == "WS-OK"
    finally:
        await runner.cleanup()


import asyncio  # noqa: E402


def test_ws_roundtrip(ws_app):
    asyncio.run(_roundtrip(ws_app, aiohttp))


async def _slash(ws_app, aiohttp):
    app, session = ws_app
    from aiohttp import web
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    notes = []
    try:
        async with aiohttp.ClientSession() as client:
            async with client.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
                await asyncio.wait_for(ws.receive_str(), 5)                  # history hello
                for cmd in ("/plan", "/usage", "/undo"):
                    await ws.send_str(json.dumps({"t": "send", "text": cmd}))
                    while True:
                        data = json.loads(await asyncio.wait_for(ws.receive_str(), 5))
                        assert data["t"] != "text", "slash command reached the model"
                        if data["t"] == "note":
                            notes.append(data["text"])
                            break
            async with client.get(f"http://127.0.0.1:{port}/api/commands") as resp:
                names = [c["name"] for c in (await resp.json())["commands"]]
    finally:
        await runner.cleanup()
    return notes, names, session


def test_web_slash_commands(ws_app):
    notes, names, session = asyncio.run(_slash(ws_app, aiohttp))
    assert "plan mode ON" in notes[0] and session.plan_mode
    assert "request(s)" in notes[1] and "nothing to undo" in notes[2]
    assert {"model", "undo", "plan", "usage"} <= set(names)
