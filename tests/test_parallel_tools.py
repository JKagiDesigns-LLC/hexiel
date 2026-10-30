"""Parallel tool execution: a whole read-only batch of one step runs concurrently, one failing
call carries its own error result (its siblings survive), and anything needing an approval or
writing files keeps the sequential UI contract."""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.config import ModelConfig, Settings
from hexiel.tools import ToolDef, ToolResult, ToolContext


def make_tool(name: str, seconds: float = 0.0, fail: bool = False, readonly: bool = True, log: list = None):
    def run(args, ctx):
        if seconds:
            time.sleep(seconds)
        if fail:
            raise RuntimeError(f"boom-{name}")
        if log is not None:
            log.append(name)
        return ToolResult(text=f"{name} ok")
    return ToolDef(name, "test tool", {}, readonly=readonly, run=run)


def turn_session(tmp_path, tools, calls):
    s = Session(ModelConfig(name="m", provider="openai", model="m", base_url="https://cloud.example.com"),
                Settings(), str(tmp_path), tools, "sys")
    s.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    events = []
    s.on_event = lambda e, *p, **k: events.append((e, *p))
    outcomes = iter([{"role": "assistant", "tool_calls": calls},
                     {"role": "assistant", "content": "batch done"}])
    monkeypatch_target = Session
    return s, events, outcomes, monkeypatch_target


def run_with(monkeypatch, s, outcomes):
    monkeypatch.setattr(Session, "_stream_once",
                        lambda self, provider, tool_defs: next(outcomes))
    monkeypatch.setattr("hexiel.llm.make_provider", lambda *a, **k: object())
    s.run_turn("use the tools")


def test_two_reads_run_concurrently_and_land_in_call_order(tmp_path, monkeypatch):
    starts = []
    def run(args, ctx):
        starts.append((args["tool_id"], time.monotonic()))
        time.sleep(0.6)
        return ToolResult(text=f"result {args['tool_id']}")
    tools = [ToolDef(f"t{i}", "test tool",
                     {"type": "object", "properties": {"tool_id": {"type": "string"}}},
                     readonly=True, run=run) for i in (1, 2)]
    s, events, outcomes, _S = turn_session(tmp_path, tools,
                                 [{"id": "c1", "name": "t1", "arguments": {"tool_id": "a"}},
                                  {"id": "c2", "name": "t2", "arguments": {"tool_id": "b"}}])
    t0 = time.monotonic()
    run_with(monkeypatch, s, outcomes)
    wall = time.monotonic() - t0
    assert wall < 1.0                                   # sequential would be ≥ 1.2 s (two 0.6 s runs)
    assert starts[1][1] - starts[0][1] < 0.5            # both started together, not one after the other
    entries = [m for m in s.messages if m["role"] == "tool"]
    assert [m["content"] for m in entries] == ["result a", "result b"]      # call order preserved
    assert s.messages[-1]["content"] == "batch done"


def test_a_failed_sibling_does_not_cancel_the_batch(tmp_path, monkeypatch):
    tools = [ToolDef("bad", "test tool", {"type": "object", "properties": {}}, readonly=True,
                     run=lambda args, ctx: (_ for _ in ()).throw(RuntimeError("boom"))),
             ToolDef("good", "test tool", {"type": "object", "properties": {}}, readonly=True,
                     run=lambda args, ctx: ToolResult(text="good ok"))]
    s, events, outcomes, _S = turn_session(tmp_path, tools,
                                 [{"id": "c1", "name": "bad", "arguments": {}},
                                  {"id": "c2", "name": "good", "arguments": {}}])
    run_with(monkeypatch, s, outcomes)
    tools_msg = [m for m in s.messages if m["role"] == "tool"]
    assert tools_msg[0]["is_error"] and "boom" in tools_msg[0]["content"]        # its own error, contained
    assert tools_msg[1]["content"] == "good ok"                                  # the sibling ran
    assert s.messages[-1]["content"] == "batch done"


def test_a_batch_with_a_write_stays_sequential_and_asks(tmp_path, monkeypatch):
    approvals = []
    def write_run(args, ctx):
        Path(ctx.cwd).joinpath("out.txt").write_text("written")
        return ToolResult(text="wrote")
    tools = [ToolDef("myread", "test tool", {"type": "object", "properties": {}}, readonly=True,
                     run=lambda args, ctx: ToolResult(text="read ok")),
             ToolDef("mywrite", "test tool", {"type": "object", "properties": {}}, run=write_run)]
    s, events, outcomes, _S = turn_session(tmp_path, tools,
                                 [{"id": "c1", "name": "myread", "arguments": {}},
                                  {"id": "c2", "name": "mywrite", "arguments": {}}])
    s.on_approve = lambda tool, args: approvals.append(tool) or "yes"
    assert s._parallel_safe([{"id": "c2", "name": "mywrite", "arguments": {}}]) is False
    run_with(monkeypatch, s, outcomes)
    assert approvals == ["mywrite"]                                              # asked on the main thread, once
    assert (Path(s.cwd) / "out.txt").read_text() == "written"


def test_parallel_safe_allows_readonly_and_refuses_ask(tmp_path, monkeypatch):
    from hexiel.session import Session as S
    from hexiel.config import ModelConfig as MC, Settings as ST
    s = S(MC(name="m", provider="openai", model="m", base_url="https://cloud.example.com"),
          ST(), str(tmp_path), [], "sys")
    s.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    s.tools = [ToolDef("t", "test tool", {"type": "object", "properties": {}}, readonly=True,
                       run=lambda args, ctx: ToolResult(text="x")),
               ToolDef("w", "test tool", {"type": "object", "properties": {}},
                       run=lambda args, ctx: ToolResult(text="x"))]
    assert s._parallel_safe([{"id": "1", "name": "t", "arguments": {}},
                             {"id": "2", "name": "t", "arguments": {}}]) is True
    assert s._parallel_safe([{"id": "1", "name": "w", "arguments": {}}]) is False   # a write without auto-approve