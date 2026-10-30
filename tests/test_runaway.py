"""Runaway-stop guards (Cline's mistake budget + loop detector as a hard stop): a turn whose tool
calls keep failing or repeat the very same call ends early with state preserved and a resumable
message instead of grinding until the timeouts."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import consult
from hexiel.session import Session
from hexiel.config import ModelConfig, Settings
from hexiel.tools import ToolDef, ToolResult, ToolContext
from hexiel.tooling import Toolchain


def failing_tool(name="myread"):
    def run(args, ctx):
        raise RuntimeError("still failing")
    return ToolDef(name, "test tool", {"type": "object", "properties": {}}, readonly=True, run=run)


def ok_tool(name="myread"):
    def run(args, ctx):
        return ToolResult(text="fine")
    return ToolDef(name, "test tool", {"type": "object", "properties": {}}, readonly=True, run=run)


def make(tmp_path, tool):
    s = Session(ModelConfig(name="m", provider="openai", model="m", base_url="https://cloud.example.com"),
                Settings(), str(tmp_path), [tool], "sys")
    s.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    events = []
    s.on_event = lambda e, *p, **k: events.append((e, *p))
    s.on_approve = lambda *a, **k: "yes"
    return s, events


def run_turns(monkeypatch, s, outcomes):
    monkeypatch.setattr(Session, "_stream_once", lambda self, provider, td: next(outcomes))
    monkeypatch.setattr("hexiel.llm.make_provider", lambda *a, **k: object())
    s.run_turn("do the thing")


def test_six_failures_in_a_row_end_the_turn_with_state_preserved(tmp_path, monkeypatch):
    s, events = make(tmp_path, failing_tool())
    calls = [{"id": f"c{n}", "name": "myread", "arguments": {}} for n in range(7)]   # 7 identical failing calls
    run_turns(monkeypatch, s, iter([{"role": "assistant", "tool_calls": calls},
                                    {"role": "assistant", "content": "never reached"}]))
    ends = [e for e in events if e[0] == "turn_end"]
    assert ends and "tool calls in a row failed" in str(ends[-1]) and "State preserved" in str(ends[-1])
    assert any(e[0] == "note" and "🛑" in str(e[1]) for e in events)
    assert len([m for m in s.messages if m["role"] == "tool"]) == 7         # the batch landed, then the turn ended
    assert s.messages[-1]["content"] != "never reached"                     # the next stream never ran


def test_five_identical_calls_end_the_turn_as_a_loop(tmp_path, monkeypatch):
    s, events = make(tmp_path, ok_tool())
    calls = [{"id": f"c{n}", "name": "myread", "arguments": {"x": 1}} for n in range(5)]
    run_turns(monkeypatch, s, iter([{"role": "assistant", "tool_calls": calls},
                                    {"role": "assistant", "content": "never reached"}]))
    ends = [e for e in events if e[0] == "turn_end"]
    assert "myread call was repeated 5 times" in str(ends[-1])


def test_progress_cools_the_budget(tmp_path, monkeypatch):
    s, events = make(tmp_path, ok_tool())
    calls = [{"id": f"c{n}", "name": "myread", "arguments": {"x": n}} for n in range(5)]   # each call different
    run_turns(monkeypatch, s, iter([{"role": "assistant", "tool_calls": calls},
                                    {"role": "assistant", "content": "clean stop, no runaway"}]))
    ends = [e for e in events if e[0] == "turn_end"]
    assert ends and "stopped here" not in str(ends[-1]) and s.messages[-1]["content"] == "clean stop, no runaway"


def test_a_runaway_turn_does_not_poison_the_next_one(tmp_path, monkeypatch):
    s, events = make(tmp_path, failing_tool())
    first = iter([{"role": "assistant", "tool_calls": [{"id": "c1", "name": "myread", "arguments": {}} for _ in range(6)]}])
    run_turns(monkeypatch, s, first)
    assert any(e[0] == "turn_end" and "in a row failed" in str(e[1]) for e in events)
    events.clear()
    second = iter([{"role": "assistant", "content": "new turn works fine"}])
    run_turns(monkeypatch, s, second)
    ends = [e for e in events if e[0] == "turn_end"]
    assert ends and "stopped here" not in str(ends[-1])             # the new turn starts with a fresh budget