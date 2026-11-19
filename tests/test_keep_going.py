"""A multi-step job must not stall after one step to wait for the user (Hexiel Remote, 2026-11-19: one task of
11 done, then it waited). While the todo list the session wrote has open steps, a reply with no tool call is
answered with a keep-going nudge — a bounded number of times in a row, so a genuine question still reaches the user."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel.config import ModelConfig, Settings
from hexiel.session import Session
from hexiel.tools import ToolContext, build_tools
from hexiel.tooling import Toolchain


def make(tmp_path, monkeypatch, replies, **settings):
    s = Session(ModelConfig(name="m", provider="openai", model="m", base_url="http://x"),
                Settings(**settings), str(tmp_path), build_tools(Toolchain()), "sys")
    s.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    events = []
    s.on_event = lambda e, *p, **k: events.append(e)
    seq = iter(replies)
    monkeypatch.setattr(Session, "_stream_once", lambda self, provider, tool_defs: next(seq))
    monkeypatch.setattr("hexiel.llm.make_provider", lambda *a, **k: object())
    return s, events


def todo_call(action, **args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"c{action}", "name": "todo", "arguments": {"action": action, **args}}]}


def say(text):
    return {"role": "assistant", "content": text}


def test_continues_past_a_step_boundary(tmp_path, monkeypatch):
    s, events = make(tmp_path, monkeypatch, [
        todo_call("create", title="job", steps=["one", "two"]),
        say("Step one is done. Shall I continue with step two?"),      # would have ended the turn here
        todo_call("update", step="one", state="x"),
        todo_call("update", step="two", state="x"),
        say("All done.")])
    s.run_turn("do one and two")
    assert s.messages[-1]["content"] == "All done."
    assert events.count("note") >= 1


def test_stops_when_the_model_really_is_blocked(tmp_path, monkeypatch):
    s, events = make(tmp_path, monkeypatch, [
        todo_call("create", title="job", steps=["one", "two"]),
        say("Which database?"), say("I need to know which database."), say("Still need the database name."),
        say("never reached")])
    s.run_turn("do one and two")
    assert s.messages[-1]["content"] == "Still need the database name."   # 2 nudges, then the user gets the floor


def test_no_nudge_for_a_stale_todo_file_or_when_off(tmp_path, monkeypatch):
    (tmp_path / ".hexiel").mkdir()
    (tmp_path / ".hexiel" / "todo.md").write_text("# todo — old\n\n- [ ] left over\n")
    s, _ = make(tmp_path, monkeypatch, [say("hello")])
    s.run_turn("hi")                       # the todo tool was never used in this session: the file is not ours
    assert s.messages[-1]["content"] == "hello"
    s2, _ = make(tmp_path / "b", monkeypatch, [todo_call("create", title="j", steps=["a"]), say("ok")], keep_going=False)
    s2.cwd = str(tmp_path)
    s2.run_turn("x")
    assert s2.messages[-1]["content"] == "ok"
