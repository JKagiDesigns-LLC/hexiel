"""Hexiel Remote client: path allow-list, config perms, and a full tunnel round trip against a
minimal in-process relay (the real relay lives in the private hexiel-cloud repo)."""

import asyncio
import base64
import json
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import remote

aiohttp = pytest.importorskip("aiohttp")


def test_only_web_ui_paths_cross_the_tunnel():
    for ok in ("/", "/ws", "/static/app.js", "/api/state?x=1"):
        assert remote.path_allowed(ok)
    for bad in ("/v1/chat/completions", "/etc/passwd", "/static/../secret", "/api/../v1/x", "//evil"):
        assert not remote.path_allowed(bad)


def test_config_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path))
    remote.save_config({"device_token": "hxd_x"})
    assert stat.S_IMODE(remote.config_file().stat().st_mode) == 0o600
    assert remote.load_config()["device_token"] == "hxd_x"


async def _tunnel_round_trip(tmp_path):
    from aiohttp import web
    from hexiel.config import ModelConfig, Settings
    from hexiel.session import Session
    from hexiel.tooling import Toolchain
    from hexiel.tools import ToolContext, build_tools
    from hexiel.webui import make_app

    session = Session(ModelConfig(name="t", provider="openai", model="m", base_url="http://127.0.0.1:9/v1"),
                      Settings(), cwd=str(tmp_path), tools=build_tools(Toolchain()), system_prompt="t")
    session.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    runner = web.AppRunner(make_app(session))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    got = {}

    async def relay_ws(request):          # stands in for the relay: asks the device for two things
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"t": "http", "id": "1", "method": "GET", "path": "/api/state", "headers": {}, "body": ""})
        await ws.send_json({"t": "http", "id": "2", "method": "GET", "path": "/v1/models", "headers": {}, "body": ""})
        async for msg in ws:
            f = json.loads(msg.data)
            got[f.get("id") or f["t"]] = f
            if "1" in got and "2" in got:
                await ws.close()
        return ws

    relay = web.Application()
    relay.router.add_get("/ws/device", relay_ws)
    rrunner = web.AppRunner(relay)
    await rrunner.setup()
    rsite = web.TCPSite(rrunner, "127.0.0.1", 0)
    await rsite.start()
    rport = rsite._server.sockets[0].getsockname()[1]

    tunnel = remote.Tunnel({"relay": f"http://127.0.0.1:{rport}", "device_token": "t", "name": "x"},
                           f"http://127.0.0.1:{port}", str(tmp_path), session)
    task = asyncio.create_task(tunnel.run_forever())
    for _ in range(100):
        if "1" in got and "2" in got:
            break
        await asyncio.sleep(0.05)
    tunnel.stop.set()
    await asyncio.wait_for(task, 5)
    await runner.cleanup()
    await rrunner.cleanup()
    assert got["1"]["status"] == 200 and "status" in json.loads(base64.b64decode(got["1"]["body"]))
    assert got["2"]["status"] == 404          # /v1 never crosses the tunnel


def test_tunnel_round_trip(tmp_path):
    asyncio.run(_tunnel_round_trip(tmp_path))
