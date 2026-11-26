"""'Make a plan and wait for my input' must wait (review hold), plan mode hands off to build through plan_exit, and
subagents share findings / are not mistaken for other sessions."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import findings, hooks
from hexiel.session import review_hold_after
from test_keep_going import make, say, todo_call


def test_hold_phrases():
    for text in ("make a plan and wait for my input", "plan it, don't start until I approve", "let me review it first",
                 "just make a plan", "ask me first before you change anything"):
        assert review_hold_after(text, False), text
    for text in ("fix the login bug", "please wait for the build to finish"):
        assert not review_hold_after(text, False), text


def test_hold_survives_revisions_and_ends_on_approval():
    assert review_hold_after("change step 3 to use sqlite", True)
    assert not review_hold_after("looks good, go ahead", True)
    assert not review_hold_after("an unrelated question " * 30, True)


def test_review_hold_does_not_carry_on_past_the_plan(tmp_path, monkeypatch):
    s, events = make(tmp_path, monkeypatch, [
        todo_call("create", title="job", steps=["one", "two"]),
        say("Here is the plan. Tell me when to go.")])
    s.run_turn("make a plan for one and two and wait for my input")
    assert s.messages[-1]["content"].startswith("Here is the plan")
    assert not any("carrying on" in str(e) for e in events)
    assert s.review_hold


def test_write_tools_refused_while_held(tmp_path, monkeypatch):
    s, _ = make(tmp_path, monkeypatch, [])
    s.review_hold = True
    call = {"id": "w", "name": "write", "arguments": {"path": str(tmp_path / "x.txt"), "content": "hi"}}
    *_, refusal = s._screen(call)
    assert refusal and refusal[1] and "review the plan" in refusal[0]


def test_plan_exit_approval_switches_to_build(tmp_path, monkeypatch):
    s, _ = make(tmp_path, monkeypatch, [])
    s.plan_mode = True
    s.on_approve = lambda *a: "yes"
    assert "Approved" in s.plan_exit({"plan": "1. do it"}).text
    assert not s.plan_mode


def test_plan_exit_declined_stays_in_plan(tmp_path, monkeypatch):
    s, _ = make(tmp_path, monkeypatch, [])
    s.plan_mode = True
    s.on_approve = lambda *a: "no"
    assert "did not approve" in s.plan_exit({"plan": "1. do it"}).text
    assert s.plan_mode


def test_plan_exit_headless_never_self_approves(tmp_path, monkeypatch):
    s, _ = make(tmp_path, monkeypatch, [])
    s.plan_mode = True
    assert "wait for the user" in s.plan_exit({"plan": "1. do it"}).text
    assert s.plan_mode
    s.plan_mode = False
    assert s.plan_exit({"plan": "x"}).is_error


def test_findings_board():
    b = findings.Findings()
    assert b.post("a", "cfg", "x lives in /a.py") == "shared as #1"
    assert b.post("a", "cfg", "x lives in /a.py").startswith("already")
    assert "x lives" in b.render(exclude="b") and b.render(exclude="a") == ""


def test_hook_payload_links_helper_to_parent(monkeypatch):
    seen = {}

    class P:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(hooks.subprocess, "run", lambda cmd, **kw: seen.update(json=kw["input"]) or P())
    h = hooks.Hook(event="Stop", command="true", matcher="", timeout=5) if hasattr(hooks, "Hook") else None
    if h is None:
        return
    hooks.run([h], "Stop", ".", "child", parent_session_id="parent")
    assert '"parent_session_id": "parent"' in seen["json"]
