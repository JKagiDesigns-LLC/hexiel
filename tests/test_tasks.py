"""Background shell tasks: run_in_background, timeout parking (the command keeps running as a
task instead of being killed), and the tasks tool (list / output / kill)."""

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel import tools as tools_mod
from hexiel.tools import ToolContext, _run_bash, _tasks_tool, drain_finished, _TASKS, _register_task, ShellTask
from hexiel.config import ModelConfig, Settings


def ctx_for(tmp_path):
    return ToolContext(cwd=str(tmp_path), chain=Toolchain())


@pytest.fixture(autouse=True)
def fresh_tasks():
    """Each test gets an empty task table: the registry is process-wide by design."""
    _TASKS.clear()
    yield
    _TASKS.clear()


def wait_until(fn, timeout=15.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return False


def task_id_from(text: str) -> str:
    return next(w for w in text.split() if w.startswith("bg-"))


def test_run_in_background_starts_a_task_and_reports_its_finish(tmp_path):
    ctx = ctx_for(tmp_path)
    r = _run_bash({"command": "echo hello-task", "run_in_background": True}, ctx)
    assert not r.is_error
    tid = task_id_from(r.text)
    assert wait_until(lambda: _TASKS[tid].done)
    out = _tasks_tool({"op": "output", "id": tid}, ctx)
    assert "hello-task" in out.text and "(finished, exit code 0)" in out.text
    notes = drain_finished()
    assert len(notes) == 1 and tid in notes[0] and "finished" in notes[0]
    assert drain_finished() == []                      # reported once, not again


def test_a_timeout_parks_the_command_instead_of_killing_it(tmp_path):
    ctx = ctx_for(tmp_path)
    r = _run_bash({"command": "sleep 2 && echo the-late-line", "timeout": 0.6}, ctx)
    assert not r.is_error and "parked as background task" in r.text
    tid = task_id_from(r.text)
    assert wait_until(lambda: "the-late-line" in _TASKS[tid].text())
    assert _TASKS[tid].done
    note = drain_finished()[0]
    assert tid in note and "finished" in note


def test_tasks_list_shows_a_running_task_and_kill_stops_it(tmp_path):
    ctx = ctx_for(tmp_path)
    r = _run_bash({"command": "sleep 30", "run_in_background": True}, ctx)
    tid = task_id_from(r.text)
    listing = _tasks_tool({"op": "list"}, ctx)
    assert tid in listing.text and "running" in listing.text
    out = _tasks_tool({"op": "output", "id": tid}, ctx)
    # a quiet start says how long it has been running instead of a bare "(no output yet)":
    # the bare string made the model re-poll until the loop guard ended the whole turn
    assert "no output yet" in out.text and "still running" in out.text
    killed = _tasks_tool({"op": "kill", "id": tid}, ctx)
    assert not killed.is_error and "killed" in killed.text
    assert wait_until(lambda: _TASKS[tid].done)


def test_a_completed_task_reaches_the_model_as_a_message(tmp_path, monkeypatch):
    done = ShellTask("bg-7", "bash: echo done", _FakeProc(), time.time() - 3, out=["all finished\n"], done=True)
    _register_task(done)
    s = Session(ModelConfig(name="m", provider="openai", model="m", base_url="http://x.local"),
                Settings(), str(tmp_path), [], "sys")
    events = []
    s.on_event = lambda e, *p, **k: events.append((e, *p))
    outcomes = iter([{"role": "assistant", "content": "got it, task read"}])
    monkeypatch.setattr(Session, "_stream_once", lambda self, provider, tool_defs: next(outcomes))
    monkeypatch.setattr("hexiel.llm.make_provider", lambda *a, **k: object())
    s.run_turn("wait for the task")
    task_msgs = [m for m in s.messages if m["role"] == "user" and str(m.get("content", "")).startswith("[system] background task")]
    assert any("bg-7" in m["content"] for m in task_msgs)      # other tests' finished tasks may drain alongside
    assert any(e[0] == "note" and "bg-7" in str(e[1]) for e in events)


class _FakeProc:
    code = 0