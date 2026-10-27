"""A chat saved while a tool was running (hexiel killed or restarted mid-command) must still be
sendable: every tool call needs a result right after it, or Anthropic answers 400
"tool_use ids were found without tool_result blocks immediately after"."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import llm
from hexiel.session import repair_tool_pairs


def _call(cid, name="bash"):
    return {"id": cid, "name": name, "arguments": {}}


def test_unanswered_calls_get_results_before_the_next_user_message():
    msgs = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "", "tool_calls": [_call("a"), _call("b"), _call("c")]},
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
        {"role": "user", "content": "continue"},
    ]
    assert repair_tool_pairs(msgs)
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "tool", "tool", "user"]
    assert [m["tool_call_id"] for m in msgs[2:5]] == ["a", "b", "c"]
    assert msgs[3]["is_error"] and "interrupted" in msgs[3]["content"]

    converted = llm.AnthropicProvider._convert_messages(msgs)
    results = converted[2]["content"]
    assert [b["tool_use_id"] for b in results] == ["a", "b", "c"]


def test_calls_at_the_end_of_the_chat_are_answered():
    msgs = [{"role": "user", "content": "x"},
            {"role": "assistant", "content": "", "tool_calls": [_call("a")]}]
    assert repair_tool_pairs(msgs)
    assert msgs[-1]["tool_call_id"] == "a"


def test_stray_and_duplicate_results_are_dropped():
    msgs = [
        {"role": "tool", "tool_call_id": "old", "content": "orphan"},
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "", "tool_calls": [_call("a")]},
        {"role": "tool", "tool_call_id": "a", "content": "1"},
        {"role": "tool", "tool_call_id": "a", "content": "dup"},
        {"role": "tool", "tool_call_id": "zzz", "content": "wrong"},
    ]
    assert repair_tool_pairs(msgs)
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool"]
    assert msgs[2]["content"] == "1"


def test_a_healthy_chat_is_left_alone():
    msgs = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "", "tool_calls": [_call("a"), _call("b")]},
        {"role": "tool", "tool_call_id": "a", "content": "1"},
        {"role": "graphic", "content": "card"},
        {"role": "tool", "tool_call_id": "b", "content": "2"},
        {"role": "assistant", "content": "done"},
    ]
    before = [dict(m) for m in msgs]
    assert not repair_tool_pairs(msgs)
    assert msgs == before
