"""Truncated model output: a reply cut off at max_output_tokens is stamped (_truncated) and the
turn asks the model to continue instead of treating the cut reply as the answer."""

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import llm
from hexiel import session as session_mod
from hexiel.config import ModelConfig, Settings
from hexiel.session import Session


def sse(*chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def openai_stream(monkeypatch, body: bytes):
    provider = llm.OpenAICompatProvider("k", base_url="http://x/v1")
    monkeypatch.setattr(llm.AbortableHTTP, "client", lambda self, timeout: httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}))))
    events = list(provider.stream([{"role": "user", "content": "go"}], "sys", [], model="m",
                                  max_output_tokens=100, extra_body={}, timeout=5))
    final = next(p[0] for e, *p in events if e == "assistant")
    return events, final


# ---------------------------------------------------------------- provider level

def test_a_length_finish_reason_stamps_the_reply(monkeypatch):
    body = sse({"choices": [{"index": 0, "delta": {"content": "half of the fi"},
                             "finish_reason": None}]},
               {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]})
    _, final = openai_stream(monkeypatch, body)
    assert final["_truncated"] is True


def test_a_normal_stop_is_not_truncated(monkeypatch):
    body = sse({"choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}]},
               {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    _, final = openai_stream(monkeypatch, body)
    assert "_truncated" not in final


def test_anthropic_max_tokens_stamps_the_reply(monkeypatch):
    events_bytes = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in [
        {"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "partial"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 99}},
        {"type": "message_stop"},
    ]).encode()
    provider = llm.AnthropicProvider("key", base_url="http://anthropic.test")
    monkeypatch.setattr(llm.AbortableHTTP, "client", lambda self, timeout: httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, content=events_bytes, headers={"content-type": "text/event-stream"}))))
    events = list(provider.stream([{"role": "user", "content": "go"}], "sys", [], model="m",
                                  max_output_tokens=100, extra_body={}, timeout=5))
    final = next(p[0] for e, *p in events if e == "assistant")
    assert final["content"] == "partial" and final["_truncated"] is True


# ---------------------------------------------------------------- turn level

@pytest.fixture
def clock(monkeypatch):
    """Instant time: sleep advances a fake clock."""
    now = [1_000_000.0]
    monkeypatch.setattr(session_mod.time, "time", lambda: now[0])
    monkeypatch.setattr(session_mod.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    return now


def turn_session(tmp_path, monkeypatch, outcomes, **settings):
    s = Session(ModelConfig(name="m", provider="openai", model="m", base_url="http://x.local"),
                Settings(**settings), str(tmp_path), [], "sys")
    events = []
    s.on_event = lambda e, *p, **k: events.append((e, *p))
    seq = iter(outcomes)

    def fake_once(self, provider, tool_defs):
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item
    monkeypatch.setattr(Session, "_stream_once", fake_once)
    monkeypatch.setattr("hexiel.llm.make_provider", lambda *a, **k: object())
    return s, events


def test_a_truncated_reply_gets_one_continuation_request(tmp_path, monkeypatch, clock):
    s, events = turn_session(tmp_path, monkeypatch, [
        {"role": "assistant", "content": "half of the file", "_truncated": True},
        {"role": "assistant", "content": "…second half, all finished"}])
    s.run_turn("write the file")
    nudges = [m for m in s.messages if m["role"] == "user"
              and "Continue from exactly where it stopped" in (m.get("content") or "")]
    assert s.messages[-1]["content"].startswith("…second half")
    assert len(nudges) == 1 and any("token cap" in str(e[1]) for e in events if e[0] == "note")


def test_the_nudge_gives_up_after_two_and_notes_the_cut(tmp_path, monkeypatch, clock):
    s, events = turn_session(tmp_path, monkeypatch, [
        {"role": "assistant", "content": f"chunk {i}", "_truncated": True} for i in range(3)])
    s.run_turn("write the file")
    nudges = [m for m in s.messages if m["role"] == "user"
              and "Continue from exactly where it stopped" in (m.get("content") or "")]
    assert len(nudges) == 2                                     # 2 continuations, then hexiel stops asking
    assert s.messages[-1]["content"] == "chunk 2"
    assert any("max_output_tokens" in str(e[1]) for e in events if e[0] == "note")


def test_a_truncated_reply_with_tool_calls_is_left_to_the_tool_flow(tmp_path, monkeypatch, clock):
    s, events = turn_session(tmp_path, monkeypatch, [
        {"role": "assistant", "content": "starting", "_truncated": True,
         "tool_calls": [{"id": "c1", "name": "read", "arguments": {"path": "a.py"}}]},
        {"role": "assistant", "content": "done"}])
    s.run_turn("continue the work")
    assert not [m for m in s.messages if m["role"] == "user"
                and "Continue from exactly where it stopped" in (m.get("content") or "")]