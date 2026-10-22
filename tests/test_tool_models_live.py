"""Tool models against a real HTTP model server (a fake OpenAI-compatible one, in a thread): streaming, slow starts, slow
streams, server errors, caching, two sessions sharing one model, and stopping a digest mid-flight."""

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import digest, modelbusy
from hexiel.config import ModelConfig, Settings
from hexiel.tools import ToolResult
from tests.test_remote_control import make_session


class FakeModelServer:
    """POST /v1/chat/completions, streamed like Ollama/OpenAI. Behaviour is set per test."""

    def __init__(self):
        self.mode, self.delay, self.requests, self.answer = "ok", 0.0, 0, "The file defines foo() at line 12 and bar() at line 40."
        self.seen_prompts = []
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        assert self.ready.wait(10)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self.chat)
        self.runner = web.AppRunner(app)
        self.loop.run_until_complete(self.runner.setup())
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        self.loop.run_until_complete(site.start())
        self.port = site._server.sockets[0].getsockname()[1]
        self.ready.set()
        self.loop.run_forever()

    async def chat(self, request):
        body = await request.json()
        self.requests += 1
        self.seen_prompts.append(body["messages"][-1]["content"])
        if self.mode == "error":
            return web.json_response({"error": {"message": "model exploded"}}, status=500)
        resp = web.StreamResponse(headers={"content-type": "text/event-stream"})
        await resp.prepare(request)
        if self.mode == "slow_first":
            await asyncio.sleep(self.delay)
        words = self.answer.split(" ")
        for i, word in enumerate(words):
            if self.mode == "slow_stream":
                await asyncio.sleep(self.delay)
            chunk = {"choices": [{"delta": {"content": word + (" " if i < len(words) - 1 else "")}}]}
            await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
        await resp.write(b'data: {"choices": [], "usage": {"prompt_tokens": 900, "completion_tokens": 20}}\n\n')
        await resp.write(b"data: [DONE]\n\n")
        return resp

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self):
        asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)


@pytest.fixture
def server():
    s = FakeModelServer()
    yield s
    s.close()


@pytest.fixture
def setup(tmp_path, monkeypatch, server):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path / "data"))
    small = ModelConfig(name="small", provider="openai", model="tiny", base_url=server.base_url)
    big = ModelConfig(name="big", provider="openai", model="large", base_url="http://127.0.0.1:9/v1")
    from hexiel import planner
    monkeypatch.setattr(planner, "_config_profile", lambda session, name: small if name == "small" else None)

    def make(timeout=60, **kw):
        s = make_session(tmp_path)
        s.settings = Settings(tool_models={"read": "small"}, digest_timeout=timeout, **kw)
        s.model_config = big
        s.__dict__["_turn_request"] = "where is foo defined?"
        s.events = []
        s.on_event = lambda e, *p, **k: s.events.append((e, *p))
        return s
    return make


BIG = ToolResult(text="".join(f"{i}: value_{i} = {i}\n" for i in range(600)))
CALL = {"id": "c1", "name": "read", "arguments": {"path": "big.py"}}


def test_a_real_streamed_answer_becomes_the_summary(setup, server):
    s = setup()
    out = digest.maybe_digest(s, CALL, BIG)
    assert out.text.endswith("bar() at line 40.") and out.text.startswith("[summary by small")
    assert server.requests == 1 and "where is foo defined?" in server.seen_prompts[0] and "599: value_599" in server.seen_prompts[0]
    st = digest.stats(s)
    assert st["digested"] == 1 and st["raw_chars"] == len(BIG.text) and 0 < st["kept_chars"] < st["raw_chars"]
    assert "1 output(s) condensed" in digest.summary_line(s) and "tokens kept out" in digest.summary_line(s)


def test_the_same_output_is_not_summarised_twice(setup, server):
    s = setup()
    first = digest.maybe_digest(s, CALL, BIG)
    second = digest.maybe_digest(s, {**CALL, "id": "c2"}, BIG)
    assert first.text == second.text and server.requests == 1 and digest.stats(s)["cached"] == 1
    s.__dict__["_turn_request"] = "a different question"                       # a different request needs a fresh digest
    digest.maybe_digest(s, CALL, BIG)
    assert server.requests == 2


def test_a_model_that_never_starts_is_given_up_on(setup, server):
    server.mode, server.delay = "slow_first", 4
    s = setup(timeout=1)
    began = time.time()
    out = digest.maybe_digest(s, CALL, BIG)
    assert out is BIG and time.time() - began < 3.5                             # raw output, well before the model would have answered
    assert any("took too long" in str(e) for e in s.events) and digest.stats(s)["fallbacks"] == 1


def test_a_model_that_streams_too_slowly_is_given_up_on(setup, server):
    server.mode, server.delay = "slow_stream", 0.4
    s = setup(timeout=1)
    began = time.time()
    assert digest.maybe_digest(s, CALL, BIG) is BIG and time.time() - began < 3
    assert any("took too long" in str(e) for e in s.events)


def test_a_server_error_falls_back_to_the_raw_output(setup, server):
    server.mode = "error"
    s = setup()
    assert digest.maybe_digest(s, CALL, BIG) is BIG
    assert any("couldn't summarise" in str(e) for e in s.events) and digest.stats(s)["fallbacks"] == 1


def test_a_dead_server_falls_back_too(setup, server):
    s = setup()
    server.close()
    server.runner = type("R", (), {"cleanup": staticmethod(lambda: asyncio.sleep(0))})()      # fixture close() will be a no-op
    server.loop = asyncio.new_event_loop()
    threading.Thread(target=server.loop.run_forever, daemon=True).start()
    assert digest.maybe_digest(s, CALL, BIG) is BIG
    assert any("couldn't summarise" in str(e) for e in s.events)


def test_the_marker_is_cleared_after_every_outcome(setup, server):
    s = setup()
    digest.maybe_digest(s, CALL, BIG)
    server.mode = "error"
    digest.maybe_digest(s, {**CALL, "arguments": {"path": "other.py"}}, ToolResult(text="y" * 5000))
    assert modelbusy.others(ModelConfig(name="small", provider="openai", model="tiny", base_url=server.base_url)) == 0


def test_two_sessions_sharing_the_model_are_warned(setup, server):
    server.mode, server.delay = "slow_stream", 0.15
    a, b = setup(), setup()
    t = threading.Thread(target=lambda: digest.maybe_digest(a, CALL, BIG))
    t.start()
    time.sleep(0.4)                                                              # a is mid-request
    digest.maybe_digest(b, {**CALL, "arguments": {"path": "x.py"}}, ToolResult(text="z" * 5000))
    t.join(10)
    assert any(e[0] == "note" and "busy with 1 other request" in e[1] for e in b.events)
    assert not any(e[0] == "note" and "busy with" in e[1] for e in a.events)


def test_stop_ends_a_digest_promptly(setup, server):
    server.mode, server.delay = "slow_stream", 0.5
    s = setup(timeout=60)
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("out", digest.maybe_digest(s, CALL, BIG)))
    t.start()
    time.sleep(0.8)
    began = time.time()
    s.stop()
    t.join(5)
    assert not t.is_alive() and time.time() - began < 3
    assert "out" in result                                                       # it returned (raw or partial), it did not hang


def test_the_session_hands_the_model_the_digest_not_the_file(setup, server, tmp_path):
    s = setup()
    s.save = lambda: None
    (tmp_path / "big.py").write_text(BIG.text)
    s._execute_tool({"id": "c9", "name": "read", "arguments": {"path": "big.py"}})
    tool_msg = next(m for m in s.messages if m["role"] == "tool")
    assert tool_msg["content"].startswith("[summary by small") and "value_300" not in tool_msg["content"]
    assert server.requests == 1


def test_status_text_reports_readiness_and_timing(setup, server):
    s = setup()
    text = digest.status_text(s)
    assert "read" in text and "small" in text and "✓ ready" in text and "over 3,000 chars" in text
    timed = digest.status_text(s, probe=True)
    assert "answered in" in timed
    assert "no tool models set" in digest.status_text(make_session(Path("/tmp")))


def test_toolmodels_command_in_the_web_ui(setup, server):
    pytest.importorskip("aiohttp")
    from aiohttp.test_utils import TestClient, TestServer
    from hexiel.webui import make_app
    s = setup()

    async def go():
        async with TestClient(TestServer(make_app(s))) as client:
            ws = await client.ws_connect("/ws")
            await ws.receive_json()
            await ws.send_json({"t": "send", "text": "/toolmodels"})
            for _ in range(10):
                msg = await ws.receive_json()
                if msg.get("t") == "note":
                    return msg["text"]
    assert "read" in asyncio.run(go()) and "small" in asyncio.run(go())
