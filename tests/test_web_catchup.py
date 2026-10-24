"""A browser that joins (or wakes up) mid-turn must see what a tab that never left sees: the live frames
since the turn began, and any approval still waiting. Without that a backgrounded remote tab showed nothing
for hours while a delegated plan ran, and an unseen approval timed out as a denial."""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("aiohttp")

from hexiel.config import ModelConfig, Settings
from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, build_tools
from hexiel.webui import WebState


@pytest.fixture()
def state(tmp_path):
    session = Session(ModelConfig(name="t", provider="openai", model="m", base_url="http://127.0.0.1:9/v1"),
                      Settings(), cwd=str(tmp_path), tools=build_tools(Toolchain()), system_prompt="t")
    session.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    session.save = lambda: None
    return WebState(session)


def begin_turn(st, text="do the plan"):
    st.session.messages.append({"role": "user", "content": text})
    st.on_event("turn_start")


def test_midturn_join_replays_what_it_missed(state):
    state.session.messages.append({"role": "user", "content": "earlier"})
    state.session.messages.append({"role": "assistant", "content": "ok"})
    begin_turn(state)
    state.busy = True
    state.on_event("note", "▶ step 1/3: write tests")
    state.on_event("tool_call", {"id": "a", "name": "bash", "arguments": {"command": "ls"}})
    state.on_event("tool_result", {"id": "a", "name": "bash"}, "out", False, 0.5)
    state.on_event("text", "work ")
    state.on_event("text", "continues")
    frame = state.catch_up()
    assert [i["text"] for i in frame["items"] if i["t"] == "user"] == ["earlier", "do the plan"]
    kinds = [f["t"] for f in frame["replay"]]
    assert kinds == ["note", "tool", "tool_done", "text"]
    assert frame["replay"][-1]["delta"] == "work continues"          # deltas are merged, not stored one by one


def test_idle_join_gets_plain_history(state):
    begin_turn(state)
    state.on_event("note", "▶ step 1")
    state.on_event("turn_end")
    frame = state.catch_up()
    assert frame["replay"] == [] and frame["approvals"] == []


def test_replay_is_bounded(state):
    begin_turn(state)
    state.busy = True
    for i in range(WebState.REPLAY_MAX + 50):
        state.on_event("note", f"n{i}")
    frame = state.catch_up()
    assert len(frame["replay"]) == WebState.REPLAY_MAX
    assert frame["replay"][-1]["text"] == f"n{WebState.REPLAY_MAX + 49}"


def test_pending_approval_survives_the_wait_and_is_replayed(state, monkeypatch):
    holder_seen = {}

    def ask():
        holder_seen["verdict"] = state.ask_approval("bash", {"command": "rm x"})
    t = threading.Thread(target=ask, daemon=True)
    t.start()
    for _ in range(100):
        if state.approvals:
            break
        threading.Event().wait(0.02)
    frame = state.catch_up()
    assert [a["tool"] for a in frame["approvals"]] == ["bash"]
    rid = frame["approvals"][0]["id"]
    state.approvals[rid]["verdict"] = "yes"
    state.approvals[rid]["event"].set()
    t.join(3)
    assert holder_seen["verdict"] == "yes"
    assert state.catch_up()["approvals"] == []


def test_long_history_is_trimmed_to_the_tail(state):
    for i in range(WebState.HISTORY_MAX + 30):
        state.session.messages.append({"role": "user", "content": f"q{i}"})
    items = state.catch_up()["items"]
    assert len(items) == WebState.HISTORY_MAX + 1
    assert items[0]["t"] == "note" and "30 earlier" in items[0]["text"]
    assert items[-1]["text"] == f"q{WebState.HISTORY_MAX + 29}"
