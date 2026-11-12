"""Agent teams: parallel teammates that message each other mid-turn, and the board the lead and /team show."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import teams
from hexiel.config import ModelConfig, Settings
from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, ToolDef, text_result

MC = ModelConfig(name="t", provider="openai", model="m", base_url="http://127.0.0.1:9")


def lead_session(tmp_path):
    tools = [ToolDef("read", "read", {"type": "object", "properties": {}}, readonly=True,
                     run=lambda a, c: text_result("file text"))]
    s = Session(MC, Settings(False), str(tmp_path), tools, "sys")
    s.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    s.tools.append(teams.tool_def(s))
    s.save = lambda: None
    s.notes = []
    s.on_event = lambda e, *p, **k: s.notes.append(p[0]) if e == "note" else None
    return s


def scripted(monkeypatch, scripts):
    """Each teammate follows its own script; a step is a function of the helper's messages so far."""
    def stream_once(self, provider, defs):
        first = next(m["content"] for m in self.messages if m["role"] == "user")
        who = next(n for n in scripts if f'You are "{n}"' in first)
        step = sum(1 for m in self.messages if m["role"] == "assistant")
        return scripts[who][step](self)
    monkeypatch.setattr(Session, "_stream_once", stream_once)
    monkeypatch.setattr("hexiel.llm.make_provider", lambda *a, **k: object())


def call(name, **args):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": f"c-{name}-{len(args)}", "name": name,
                                                                "arguments": args}]}


def say(text):
    return {"role": "assistant", "content": text}


def test_teammates_message_each_other_while_working(tmp_path, monkeypatch):
    seen = {}

    def reviewer_final(helper):
        seen["reviewer"] = [m["content"] for m in helper.messages if m["role"] == "user"]
        got = next(c for c in seen["reviewer"] if "[team message from backend]" in c)
        return say("review done; backend said: " + got.split("] ", 1)[1])

    scripts = {
        "backend": [lambda h: call("read"),
                    lambda h: call("team_msg", to="reviewer", text="the API is GET /items?page=N"),
                    lambda h: say("backend done")],
        "reviewer": [lambda h: call("team_msg", wait=10),          # waits for the backend's message
                     reviewer_final],
    }
    scripted(monkeypatch, scripts)
    lead = lead_session(tmp_path)
    tool = next(t for t in lead.tools if t.name == "team")
    out = tool.run({"goal": "ship the items API", "members": [
        {"name": "backend", "role": "build the API", "prompt": "Build it."},
        {"name": "reviewer", "role": "review the API", "prompt": "Review it."}]}, lead.tool_ctx)
    assert not out.is_error, out.text
    assert "## backend (done)\n" in out.text and "backend done" in out.text
    assert "backend said: the API is GET /items?page=N" in out.text
    assert "backend → reviewer: the API is GET /items?page=N" in out.text          # the message log
    assert any("Team goal: ship the items API" in c for c in seen["reviewer"])
    board = lead.team_board.render()
    assert "✓ backend" in board and "✓ reviewer" in board and "2 step(s)" in board
    assert any("team backend → reviewer" in n for n in lead.notes)                 # the lead sees traffic live


def test_teammates_cannot_start_teams_or_tasks_and_research_stays_readonly(tmp_path, monkeypatch):
    names = {}

    def capture(who):
        def step(helper):
            names[who] = {t.name for t in helper.tools}
            return say("ok")
        return step
    scripted(monkeypatch, {"a": [capture("a")], "b": [capture("b")]})
    lead = lead_session(tmp_path)
    lead.tools.append(ToolDef("write", "w", {"type": "object", "properties": {}}, run=lambda a, c: text_result("")))
    teams.run_team(lead, {"members": [{"name": "a", "prompt": "x"}, {"name": "b", "prompt": "y"}]})
    for who in ("a", "b"):
        assert "team_msg" in names[who] and "read" in names[who]
        assert not {"team", "task", "write"} & names[who]


def test_board_routes_by_fuzzy_name_broadcasts_and_flags_finished_members():
    b = teams.Board([{"name": "frontend"}, {"name": "backend"}, {"name": "qa"}])
    assert b.post("qa", "Back", "hi") == "sent to backend"                 # prefix, any case
    assert b.post("qa", "all", "status?") == "sent to frontend, backend"
    b.finish("frontend", "done")
    assert "already finished" in b.post("qa", "frontend", "late note")
    assert b.post("qa", "nobody", "x").startswith("error: no teammate")
    assert b.members["backend"]["held"] == ["[team message from qa] hi", "[team message from qa] status?"]


def test_a_team_needs_two_members(tmp_path):
    lead = lead_session(tmp_path)
    assert teams.run_team(lead, {"members": [{"name": "solo", "prompt": "x"}]}).is_error
    assert teams._clean_name("a b!", {"a-b"}) == "a-b2"
