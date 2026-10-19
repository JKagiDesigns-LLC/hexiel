"""Regression: Anthropic numbers content blocks across text, thinking AND tool_use. A reply that says something and then
calls a tool has the tool at index 1; it used to create a phantom empty tool call at index 0, and the next request was
rejected with 'tool_use.name: String should have at least 1 character'."""

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import llm


def sse(*events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def start(index, block):
    return {"type": "content_block_start", "index": index, "content_block": block}


def delta(index, d):
    return {"type": "content_block_delta", "index": index, "delta": d}


def stop(index):
    return {"type": "content_block_stop", "index": index}


HEAD = {"type": "message_start", "message": {"usage": {"input_tokens": 100, "output_tokens": 1}}}
TAIL = [{"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 42}}, {"type": "message_stop"}]


def run_stream(monkeypatch, body: bytes):
    provider = llm.AnthropicProvider("key", base_url="http://anthropic.test")
    monkeypatch.setattr(llm.AbortableHTTP, "client", lambda self, timeout: httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body, headers={"content-type": "text/event-stream"}))))
    events = list(provider.stream([{"role": "user", "content": "go"}], "sys", [], model="m", max_output_tokens=100,
                                  extra_body={}, timeout=5))
    final = next(p[0] for e, *p in events if e == "assistant")
    return events, final


def test_text_then_a_tool_call_is_one_call_with_its_name(monkeypatch):
    body = sse(HEAD,
               start(0, {"type": "text", "text": ""}), delta(0, {"type": "text_delta", "text": "I'll fix it. "}), stop(0),
               start(1, {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {}}),
               delta(1, {"type": "input_json_delta", "partial_json": '{"command": "ls'}),
               delta(1, {"type": "input_json_delta", "partial_json": ' -la"}'}), stop(1), *TAIL)
    events, final = run_stream(monkeypatch, body)
    assert final["content"] == "I'll fix it. "
    assert final["tool_calls"] == [{"id": "toolu_1", "name": "bash", "arguments": {"command": "ls -la"}}]
    done = next(p[0] for e, *p in events if e == "done")
    assert done["prompt_tokens"] == 100 and done["completion_tokens"] == 42


def test_a_tool_call_with_no_text_before_it_still_works(monkeypatch):
    body = sse(HEAD, start(0, {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {}}),
               delta(0, {"type": "input_json_delta", "partial_json": '{"path": "a.py"}'}), stop(0), *TAIL)
    _, final = run_stream(monkeypatch, body)
    assert final["tool_calls"] == [{"id": "toolu_1", "name": "read", "arguments": {"path": "a.py"}}]


def test_thinking_text_and_two_tool_calls_keep_their_own_arguments(monkeypatch):
    body = sse(HEAD,
               start(0, {"type": "thinking", "thinking": ""}), delta(0, {"type": "thinking_delta", "thinking": "hmm"}), stop(0),
               start(1, {"type": "text", "text": ""}), delta(1, {"type": "text_delta", "text": "Two things."}), stop(1),
               start(2, {"type": "tool_use", "id": "toolu_a", "name": "grep", "input": {}}),
               delta(2, {"type": "input_json_delta", "partial_json": '{"pattern": "TODO"}'}), stop(2),
               start(3, {"type": "tool_use", "id": "toolu_b", "name": "read", "input": {}}),
               delta(3, {"type": "input_json_delta", "partial_json": '{"path": "b.py"}'}), stop(3), *TAIL)
    _, final = run_stream(monkeypatch, body)
    assert [(c["id"], c["name"], c["arguments"]) for c in final["tool_calls"]] == [
        ("toolu_a", "grep", {"pattern": "TODO"}), ("toolu_b", "read", {"path": "b.py"})]


def test_the_assistant_buffer_never_returns_a_nameless_call():
    buf = llm.AssistantBuffer()
    buf.start_call(2, "x", "bash")                   # a sparse index leaves gaps at 0 and 1
    buf.add_args(2, '{"command": "ls"}')
    assert buf.finalize()["tool_calls"] == [{"id": "x", "name": "bash", "arguments": {"command": "ls"}}]


def test_a_conversation_with_text_then_tool_is_accepted_when_sent_back(monkeypatch):
    """The message built from such a reply must not contain a tool_use with an empty name."""
    body = sse(HEAD, start(0, {"type": "text", "text": ""}), delta(0, {"type": "text_delta", "text": "ok"}), stop(0),
               start(1, {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {}}),
               delta(1, {"type": "input_json_delta", "partial_json": "{}"}), stop(1), *TAIL)
    _, final = run_stream(monkeypatch, body)
    blocks = llm.AnthropicProvider._convert_messages([{"role": "user", "content": "go"}, final,
                                                      {"role": "tool", "tool_call_id": "toolu_1", "content": "done"}])
    uses = [b for m in blocks for b in (m["content"] if isinstance(m["content"], list) else []) if b.get("type") == "tool_use"]
    assert len(uses) == 1 and uses[0]["name"] == "bash"
