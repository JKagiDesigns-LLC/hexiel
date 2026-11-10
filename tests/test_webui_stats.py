"""Tests for /api/stats: PyPI total-download endpoint with mocked upstreams."""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel.config import ModelConfig, Settings
from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, build_tools
from hexiel.webui import make_app

aiohttp = pytest.importorskip("aiohttp")


@pytest.fixture()
def stats_app(monkeypatch):
    tools = build_tools(Toolchain())
    ctx = ToolContext(cwd=str(Path(__file__).parent), chain=Toolchain())
    session = Session(
        ModelConfig(name="t", provider="openai", model="m", base_url="http://127.0.0.1:9/v1"),
        Settings(),
        cwd=str(Path(__file__).parent),
        tools=tools,
        system_prompt="t",
    )
    session.bind_tool_ctx(ctx)
    app = make_app(session)
    return app, session


async def _start_site(app):
    from aiohttp import web
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


async def _get_stats(port, client):
    async with client.get(f"http://127.0.0.1:{port}/api/stats") as resp:
        assert resp.status == 200
        body = await resp.read()
        headers = resp.headers
        return json.loads(body), headers


def _mock_response(status, json_data=None):
    """Return a context-manager-like object that yields a mocked response."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data)
    resp.read = AsyncMock(
        return_value=json.dumps(json_data if json_data is not None else {}).encode()
    )

    class _Ctx:
        async def __aenter__(self):
            return resp

        async def __aexit__(self, exc_type, exc, tb):
            return False

    return _Ctx()


class _FakeClientSession:
    """Minimal async context manager that routes get() calls to a handler."""

    def __init__(self, handler):
        self.handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        return self.handler(url, **kwargs)


def _run(coro):
    return asyncio.run(coro)


def _patch_client_session(handler):
    """Patch the aiohttp module webui imported, leaving the test client's real aiohttp alone."""
    import hexiel.webui as webui_mod

    return patch.object(
        webui_mod.aiohttp, "ClientSession", side_effect=lambda *a, **kw: _FakeClientSession(handler)
    )


def test_stats_pepy_total(stats_app):
    app, _ = stats_app
    pepy_data = {
        "id": "hexiel",
        "total_downloads": 999,
        "downloads": 123456,
    }

    def fake_get(url, **kwargs):
        if "pepy.tech" in url:
            return _mock_response(200, pepy_data)
        return _mock_response(404)

    async def run():
        # Create the real HTTP client before patching outbound ClientSession.
        async with aiohttp.ClientSession() as client:
            with _patch_client_session(fake_get):
                runner, port = await _start_site(app)
                try:
                    data, headers = await _get_stats(port, client)
                    assert data == {"downloads": 123456}
                    assert headers.get("Access-Control-Allow-Origin") == "*"
                finally:
                    await runner.cleanup()

    _run(run())


def test_stats_pepy_dict_summed(stats_app):
    app, _ = stats_app
    pepy_data = {
        "downloads": {
            "2026-01-01": 100,
            "2026-01-02": 250,
            "bad": "skip",
        }
    }

    def fake_get(url, **kwargs):
        if "pepy.tech" in url:
            return _mock_response(200, pepy_data)
        return _mock_response(404)

    async def run():
        async with aiohttp.ClientSession() as client:
            with _patch_client_session(fake_get):
                runner, port = await _start_site(app)
                try:
                    data, _ = await _get_stats(port, client)
                    assert data == {"downloads": 350}
                finally:
                    await runner.cleanup()

    _run(run())


def test_stats_cache_honored(stats_app):
    app, _ = stats_app
    pepy_data = {"downloads": 111}
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "pepy.tech" in url:
            return _mock_response(200, pepy_data)
        return _mock_response(404)

    async def run():
        async with aiohttp.ClientSession() as client:
            with _patch_client_session(fake_get):
                runner, port = await _start_site(app)
                try:
                    data1, _ = await _get_stats(port, client)
                    assert data1 == {"downloads": 111}
                    data2, _ = await _get_stats(port, client)
                    assert data2 == {"downloads": 111}
                    # Only one upstream pepy request inside the cache window.
                    assert sum(1 for u in calls if "pepy.tech" in u) == 1
                finally:
                    await runner.cleanup()

    _run(run())


def test_stats_pypistats_fallback(stats_app):
    app, _ = stats_app
    pypistats_data = {
        "data": [
            {"category": "with_mirrors", "downloads": 400},
            {"category": "without_mirrors", "downloads": 100},
        ]
    }
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "pepy.tech" in url:
            return _mock_response(500)
        if "pypistats.org" in url:
            return _mock_response(200, pypistats_data)
        return _mock_response(404)

    async def run():
        async with aiohttp.ClientSession() as client:
            with _patch_client_session(fake_get):
                runner, port = await _start_site(app)
                try:
                    data, _ = await _get_stats(port, client)
                    assert data == {"downloads": 500}
                    assert any("pypistats.org" in u for u in calls)
                finally:
                    await runner.cleanup()

    _run(run())


def test_stats_both_fail_return_zero(stats_app):
    app, _ = stats_app

    def fake_get(url, **kwargs):
        return _mock_response(500)

    async def run():
        async with aiohttp.ClientSession() as client:
            with _patch_client_session(fake_get):
                runner, port = await _start_site(app)
                try:
                    data, _ = await _get_stats(port, client)
                    assert data == {"downloads": 0}
                finally:
                    await runner.cleanup()

    _run(run())
