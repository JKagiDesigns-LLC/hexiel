"""The timer tool: countdowns the agent starts for long waits, shown in every view; and Stop reported as a stop."""

import json
import os
import time
from pathlib import Path

from hexiel import timers
from hexiel.config import ModelConfig, Settings
from hexiel.session import PLAN_MODE_ALLOWED, Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, build_tools


def make_session(tmp_path) -> Session:
    session = Session(ModelConfig(name="t", provider="openai", model="m", base_url="http://127.0.0.1:9/v1"),
                      Settings(), cwd=str(tmp_path), tools=build_tools(Toolchain()), system_prompt="t")
    session.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    session.tools.append(timers.tool_def(session))
    return session


def call(session, **args):
    tool = next(t for t in session.tools if t.name == "timer")
    return tool.run(args, session.tool_ctx)


def test_start_shows_a_countdown_and_stop_removes_it(tmp_path):
    session = make_session(tmp_path)
    events = []
    session.on_event = lambda event, *payload, **kw: events.append(event)
    result = call(session, action="start", label="test suite", minutes=30)
    assert not result.is_error and "30:00" in result.text
    assert "timers" in events
    timer = session.timers[0]
    assert 1799 <= timer["ends"] - time.time() <= 1800
    assert timers.summary(session) == "⏱ test suite · 30:00 left"
    result = call(session, action="stop", label="test")          # a part of the label is enough
    assert not result.is_error and session.timers == []
    assert timers.summary(session) == ""


def test_same_label_re_estimates_instead_of_adding(tmp_path):
    session = make_session(tmp_path)
    call(session, action="start", label="build", minutes=10)
    started = session.timers[0]["started"]
    result = call(session, action="start", label="Build", hours=1)
    assert "re-estimated" in result.text
    assert len(session.timers) == 1 and session.timers[0]["started"] == started
    assert session.timers[0]["ends"] - time.time() > 3500


def test_bad_input_is_an_error_for_the_model(tmp_path):
    session = make_session(tmp_path)
    assert call(session, action="start", minutes=5).is_error                       # no label
    assert call(session, action="start", label="x").is_error                        # no estimate
    assert call(session, action="start", label="x", hours=48).is_error             # too long
    assert call(session, action="start", label="x", minutes="soon").is_error
    assert call(session, action="stop", label="nothing").is_error
    assert call(session, action="dance").is_error
    for n in range(timers.MAX_TIMERS):
        assert not call(session, action="start", label=f"t{n}", minutes=1).is_error
    assert call(session, action="start", label="one more", minutes=1).is_error


def test_finished_timer_shows_over_then_drops_off(tmp_path):
    session = make_session(tmp_path)
    now = time.time()
    session.timers = [{"id": "a", "label": "tests", "started": now - 600, "ends": now - 120}]
    assert "over by 2:00" in timers.summary(session)
    session.timers = [{"id": "a", "label": "tests", "started": now - 4000, "ends": now - 3000}]
    assert timers.live(session) == [] and session.timers == []


def test_timers_survive_save_and_resume(tmp_path):
    session = make_session(tmp_path)
    call(session, action="start", label="download", minutes=20)
    saved = json.loads((Path(os.environ["HEXIEL_DATA"]) / "sessions" / f"{session.id}.json").read_text())
    assert saved["timers"][0]["label"] == "download"
    from hexiel import main
    resumed = main.build_session(str(tmp_path), None, saved)
    assert [t["label"] for t in resumed.timers] == ["download"]
    assert any(t.name == "timer" for t in resumed.tools)


def test_timer_works_in_plan_mode_and_needs_no_approval(tmp_path):
    session = make_session(tmp_path)
    assert "timer" in PLAN_MODE_ALLOWED
    assert next(t for t in session.tools if t.name == "timer").always_ok


def _frames(state) -> list[dict]:
    out = []
    while not state.queue.empty():
        item = state.queue.get_nowait()
        if item is not None:
            out.append(json.loads(item))
    return out


def test_web_status_carries_timers_with_the_machine_clock(tmp_path):
    from hexiel.webui import WebState, status_state
    session = make_session(tmp_path)
    state = WebState(session)
    call(session, action="start", label="e2e", minutes=3)
    status = [f for f in _frames(state) if f["t"] == "status"][-1]["status"]
    assert status["timers"][0]["label"] == "e2e"
    assert abs(status["now"] - time.time()) < 5
    assert status_state(session)["timers"][0]["ends"] == session.timers[0]["ends"]


def test_stop_is_reported_as_stopped_not_as_an_error(tmp_path):
    from hexiel.webui import WebState
    session = make_session(tmp_path)
    state = WebState(session)
    session.on_event("turn_end", aborted=True)
    end = [f for f in _frames(state) if f["t"] == "turn_end"][-1]
    assert end["stopped"] is True and end["error"] is None
    session.on_event("turn_end", {"error": "model unreachable"})
    end = [f for f in _frames(state) if f["t"] == "turn_end"][-1]
    assert end["stopped"] is False and end["error"] == "model unreachable"
