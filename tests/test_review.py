"""After a /delegate run the planner model reviews the diff; its findings become fix steps for the worker."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import llm, planner
from hexiel.config import Settings
from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, ToolDef
from test_delegate import PLANNER, WORKER, Planner, Worker, _write_tool, plan_json

OK = json.dumps({"verdict": "ok", "summary": "looks right", "issues": []})
FIX = json.dumps({"verdict": "fix", "summary": "one gap", "issues": [
    {"file": "a.txt", "problem": "the request also needed create fixed.txt but it is missing", "fix": "create fixed.txt"}]})


def build(tmp_path, monkeypatch, replies, review="auto", planner_profile="cloud"):
    providers = {"cloud": Planner(*replies), "local": Worker()}
    monkeypatch.setattr(llm, "make_provider", lambda mc, settings: providers[mc.name])
    monkeypatch.setattr(planner, "_config_profile", lambda session, name: {"cloud": PLANNER, "local": WORKER}.get(name))
    tools = [ToolDef("write", "w", {"type": "object", "properties": {}}, run=_write_tool)]
    s = Session(WORKER, Settings(planner_profile=planner_profile, executor_profile="local", delegate_review=review,
                                 delegate_approval=False), str(tmp_path), tools, "sys")
    s.auto_approve = True
    s.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    s.save = lambda: None
    events, shown = [], []
    s.on_event = lambda e, *p, **k: (events.append((e, *p)), shown.append(s.active_model) if e == "model_active" else None)
    return s, providers, events, shown


def notes(events):
    return [e[1] for e in events if e[0] == "note"]


def test_a_clean_review_is_reported_in_the_summary(tmp_path, monkeypatch):
    s, providers, events, shown = build(tmp_path, monkeypatch, [plan_json("a.txt"), OK])
    s.run_turn("/delegate make a file")
    assert providers["cloud"].calls == 2                                   # one plan + one review, nothing more
    assert "Review: no problems found by cloud" in s.messages[-1]["content"]
    assert any("review by cloud" in n and "no problems found" in n for n in notes(events))
    assert "big · reviewing" in shown and s.active_model == ""


def test_the_reviewer_sees_the_request_the_plan_and_the_real_diff(tmp_path, monkeypatch):
    s, providers, *_ = build(tmp_path, monkeypatch, [plan_json("a.txt"), OK])
    seen = []
    original = providers["cloud"].stream

    def spy(messages, system, tools, **kw):
        seen.append((system, messages[-1]["content"]))
        return original(messages, system, tools, **kw)
    providers["cloud"].stream = spy
    s.run_turn("/delegate make a file called a.txt")
    system, user = seen[1]
    assert system == planner.REVIEW_SYSTEM
    assert "make a file called a.txt" in user and "make a.txt" in user and "check passed" in user
    assert "+++ b/a.txt" in user and "--- /dev/null" in user                # a new file, shown as a diff from nothing


def test_problems_found_become_fix_steps_the_worker_carries_out(tmp_path, monkeypatch):
    s, providers, events, _ = build(tmp_path, monkeypatch, [plan_json("a.txt"), FIX])
    s.run_turn("/delegate make a file")
    assert (tmp_path / "a.txt").exists() and (tmp_path / "fixed.txt").exists()
    text = s.messages[-1]["content"]
    assert "Plan complete: 2/2" in text and "Review: 1 problem(s) found by cloud and fixed" in text
    assert "Fix: the request also needed" in text
    assert providers["cloud"].calls == 2                                   # review once; fixes are done by the worker
    assert any("1 problem(s) — fixing" in n for n in notes(events))


def test_a_fix_step_reuses_the_check_of_the_step_that_owned_the_file():
    plan = planner.parse_plan(plan_json("a.txt", "b.txt"), "req")
    tasks = planner.fix_tasks_from(plan, [{"file": "b.txt", "problem": "b is wrong", "fix": "redo it"},
                                          {"file": "", "problem": "something general", "fix": ""}])
    assert [t.id for t in tasks] == [3, 4] and tasks[0].check == "test -f b.txt" and tasks[1].check == ""
    assert "redo it" in tasks[0].goal and tasks[0].files == ["b.txt"]


def test_review_is_skipped_when_off(tmp_path, monkeypatch):
    s, providers, *_ = build(tmp_path, monkeypatch, [plan_json("a.txt"), OK], review="off")
    s.run_turn("/delegate make a file")
    assert providers["cloud"].calls == 1 and "Review:" not in s.messages[-1]["content"]


def test_auto_means_only_when_the_planner_is_a_different_model(tmp_path, monkeypatch):
    s, *_ = build(tmp_path, monkeypatch, [plan_json("a.txt")], review="auto", planner_profile="local")
    assert not planner.review_enabled(s)
    s.settings.delegate_review = "always"
    assert planner.review_enabled(s)
    s.settings.delegate_review = "auto"
    s.settings.planner_profile = "cloud"
    assert planner.review_enabled(s)


def test_a_review_that_fails_never_undoes_a_finished_plan(tmp_path, monkeypatch):
    import httpx
    s, providers, events, _ = build(tmp_path, monkeypatch, [plan_json("a.txt"), httpx.ConnectError("down")])
    s.run_turn("/delegate make a file")
    assert (tmp_path / "a.txt").exists() and "Plan complete: 1/1" in s.messages[-1]["content"]
    assert any("review by cloud could not be completed" in n for n in notes(events))


def test_nothing_changed_means_nothing_to_review(tmp_path, monkeypatch):
    s, *_ = build(tmp_path, monkeypatch, [plan_json("a.txt"), OK])
    plan = planner.parse_plan(plan_json("a.txt"), "req")
    assert planner.collect_changes(s, plan) == "" and planner.review_plan(s, plan) is None


def test_diffs_show_edits_to_existing_files_and_are_truncated():
    changed = planner._diff_text("x.py", b"a = 1\nb = 2\n", b"a = 1\nb = 3\n")
    assert "--- a/x.py" in changed and "-b = 2" in changed and "+b = 3" in changed
    big = planner._diff_text("big.txt", b"", ("line\n" * 5000).encode())
    assert len(big) <= planner.REVIEW_FILE_CHARS + 100 and "truncated" in big
    assert "binary file" in planner._diff_text("i.png", b"\x89PNG\xff", b"\x89PNG\xfe")


def test_review_replies_are_parsed_defensively():
    assert planner.parse_review("```json\n" + OK + "\n```")["verdict"] == "ok"
    assert planner.parse_review(json.dumps({"verdict": "fix", "issues": []}))["verdict"] == "ok"      # nothing to fix
    many = json.dumps({"verdict": "fix", "issues": [{"problem": f"p{i}"} for i in range(20)]})
    assert len(planner.parse_review(many)["issues"]) == planner.MAX_REVIEW_ISSUES
    with pytest.raises(ValueError):
        planner.parse_review(json.dumps({"verdict": "maybe"}))
    with pytest.raises(ValueError):
        planner.parse_review("no json here")


def test_review_setting_is_editable_and_validated(tmp_path, monkeypatch):
    from hexiel import settings_edit
    import hexiel.config as config
    s, *_ = build(tmp_path, monkeypatch, [plan_json("a.txt")])
    assert any(r["key"] == "settings.delegate_review" and r["label"] == "Review plans" for r in settings_edit.rows(s))
    cfg = tmp_path / "config.toml"
    cfg.write_text('[models.x]\nprovider = "openai"\nmodel = "m"\nbase_url = "http://h/v1"\n[settings]\ndelegate_review = "weird"\n')
    monkeypatch.setattr(config, "CONFIG_FILE", cfg)
    assert config.load_config().settings.delegate_review == "auto"           # unknown value -> the default
