"""/delegate: a strong model plans, a worker model does each step. Fake providers, real session machinery."""

import json
import re
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import llm, planner
from hexiel.config import ModelConfig, Settings
from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, ToolDef, text_result

PLANNER = ModelConfig(name="cloud", provider="openai", model="big", base_url="http://x")
WORKER = ModelConfig(name="local", provider="openai", model="small", base_url="http://y")


def plan_json(*files, check=True):
    return json.dumps({"summary": "make the files", "tasks": [
        {"id": i, "title": f"make {f}", "goal": f"create {f}", "files": [f],
         "check": f"test -f {f}" if check else "", "depends_on": [i - 1] if i > 1 else []}
        for i, f in enumerate(files, 1)]})


class Planner:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    def stream(self, messages, system, tools, **kw):
        self.calls += 1
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        yield ("text", reply)
        yield ("done", {"prompt_tokens": 500, "completion_tokens": 100})


class Worker:
    """Does what each step's goal says ('create X'); `wrong` lists files it gets wrong on the first try."""

    def __init__(self, wrong=(), never=()):
        self.wrong, self.never, self.prompts = set(wrong), set(never), []

    def stream(self, messages, system, tools, **kw):
        last = messages[-1]
        text = last.get("content", "") or ""
        self.prompts.append(text)
        if last["role"] == "tool":
            yield ("assistant", {"role": "assistant", "content": "created it"})
        elif "Prepare the briefing" in text or "Write the briefing" in text:
            yield ("assistant", {"role": "assistant", "content": "BRIEF: nothing special"})
        else:
            fixing = "That did not work" in text
            m = re.search(r"create (\S+)", text) or re.search(r"create (\S+)", " ".join(
                x.get("content", "") for x in messages if x["role"] == "user"))
            name = m.group(1)
            if name in self.never:
                name = "nope-" + name
            elif name in self.wrong and not fixing:
                name = "wrong-" + name
            yield ("assistant", {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "name": "write", "arguments": {"path": name, "content": "x"}}]})
        yield ("done", {"prompt_tokens": 10, "completion_tokens": 5})


def _write_tool(args, ctx):
    (Path(ctx.cwd) / args["path"]).write_text(args.get("content", ""))
    return text_result("ok")


@pytest.fixture
def env(tmp_path, monkeypatch):
    providers = {"cloud": Planner(plan_json("a.txt", "b.txt", "c.txt")), "local": Worker()}
    monkeypatch.setattr(llm, "make_provider", lambda mc, settings: providers[mc.name])
    monkeypatch.setattr(planner, "_config_profile",
                        lambda session, name: {"cloud": PLANNER, "local": WORKER}.get(name))
    tools = [ToolDef("write", "write a file", {"type": "object", "properties": {}}, run=_write_tool)]
    s = Session(WORKER, Settings(planner_profile="cloud", executor_profile="local", delegate_review="off"),
                str(tmp_path), tools, "sys")
    s.rules.allow.extend(["Write", "Bash(test *)"])         # the steps' writes run unasked; the plan questions are what these tests watch
    s.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    events, approvals = [], []
    s.on_event = lambda e, *p, **k: events.append((e, *p, k) if k else (e, *p))
    s.on_approve = lambda tool, args: (approvals.append((tool, args)) or "yes")
    s.save = lambda: None
    return s, providers, events, approvals, tmp_path


def notes(events):
    return [e[1] for e in events if e[0] == "note"]


# ---------------------------------------------------------------- triage

def test_triage_rules():
    big = ("Refactor the permission checks across all the controllers and migrate the tests:\n"
           "1. move the checks into a service\n2. update every controller\n3. rewrite the unit tests")
    assert planner.triage_score(big)[0] >= 3
    assert planner.triage_score("what does the compact_threshold setting do?")[0] <= 0
    assert planner.triage_score("rename foo to bar in utils.py")[0] <= 0


def test_unsure_requests_are_sized_by_the_worker_model(env):
    s, providers, *_ = env
    providers["local"].stream = lambda m, sy, t, **k: iter(
        [("text", '```json\n{"scale": "large", "reason": "many files"}\n```'), ("done", {})])
    ask = "implement a settings page with validation that saves the values and shows errors inline in the form"
    assert 0 < planner.triage_score(ask)[0] < 3
    assert planner.triage(s, ask) == (True, "many files")
    providers["local"].stream = lambda m, sy, t, **k: iter([("text", "no idea"), ("done", {})])
    assert planner.triage(s, ask)[0] is False                              # unparseable = a normal request


# ---------------------------------------------------------------- plan parsing

def test_parse_plan_accepts_fenced_json_and_rejects_bad_plans():
    plan = planner.parse_plan("Sure!\n```json\n" + plan_json("a.txt", "b.txt") + "\n```", "req")
    assert [t.id for t in plan.tasks] == [1, 2] and plan.tasks[1].depends_on == [1]
    for bad, why in [("no json here", "not valid JSON"), ('{"tasks": []}', "non-empty"),
                     ('{"tasks":[{"id":1,"title":"a"}]}', "title and a goal"),
                     ('{"tasks":[{"id":1,"title":"a","goal":"g"},{"id":1,"title":"b","goal":"g"}]}', "duplicate"),
                     ('{"tasks":[{"id":1,"title":"a","goal":"g","depends_on":[2]}]}', "before it"),
                     (json.dumps({"tasks": [{"id": i, "title": "t", "goal": "g"} for i in range(1, 20)]}), "too many")]:
        with pytest.raises(planner.PlanError, match=why):
            planner.parse_plan(bad, "req")


# ---------------------------------------------------------------- the whole flow

def test_delegate_runs_every_task_and_summarises(env):
    s, providers, events, approvals, tmp = env
    s.run_turn("/delegate make three files")
    assert all((tmp / f).exists() for f in ("a.txt", "b.txt", "c.txt"))
    assert "Plan complete: 3/3" in s.messages[-1]["content"] and s.messages[-1]["role"] == "assistant"
    assert [a[0] for a in approvals] == ["delegate-plan"]                    # asked once, with the plan text
    assert "make a.txt" in approvals[0][1]["text"] and "planned by cloud" in approvals[0][1]["text"]
    assert sum(1 for e in events if e[0] == "turn_end") == 1                 # helpers never end the turn early
    assert providers["cloud"].calls == 1                                     # ONE cloud call for the whole job
    saved = list((tmp / ".hexiel" / "plans").glob("*.json"))
    assert saved and all(t["status"] == "done" for t in json.loads(saved[0].read_text())["tasks"])
    todo = (tmp / ".hexiel" / "todo.md").read_text()
    assert todo.count("[x]") == 3 and "[ ]" not in todo


def test_each_task_runs_in_a_fresh_small_context(env):
    s, providers, *_ = env
    s.run_turn("/delegate make three files")
    step_prompts = [p for p in providers["local"].prompts if p.startswith("You are carrying out ONE step")]
    assert len(step_prompts) == 3
    assert "Already done" not in step_prompts[0] and "make a.txt" in step_prompts[1]     # the ledger carries forward
    assert "Step 3 of 3" in step_prompts[2] and len(step_prompts[2]) < 1500


def test_a_failing_check_is_retried_then_passes(env):
    s, providers, events, _, tmp = env
    providers["local"].wrong = {"b.txt"}
    s.run_turn("/delegate make three files")
    assert (tmp / "b.txt").exists() and "Plan complete: 3/3" in s.messages[-1]["content"]
    assert any("retrying (1/2)" in n for n in notes(events))
    assert any("That did not work" in p and "test -f b.txt" in p for p in providers["local"].prompts)


def test_a_stuck_task_is_escalated_to_the_planner_model_once(env):
    s, providers, events, _, tmp = env
    providers["local"].never = {"b.txt"}

    class Strong(Planner):
        def stream(self, messages, system, tools, **kw):
            last = messages[-1]
            if "That did not work" in (last.get("content") or ""):        # the escalated model fixes it
                yield ("assistant", {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "c9", "name": "write", "arguments": {"path": "b.txt", "content": "x"}}]})
                yield ("done", {})
            elif last["role"] == "tool":
                yield ("assistant", {"role": "assistant", "content": "fixed it myself"})
                yield ("done", {})
            else:
                yield from super().stream(messages, system, tools, **kw)

    providers["cloud"] = Strong(plan_json("a.txt", "b.txt"))
    s.run_turn("/delegate make two files")
    assert any("handing it to cloud" in n for n in notes(events))
    assert (tmp / "b.txt").exists() and "Plan complete: 2/2" in s.messages[-1]["content"]


def test_a_task_that_cannot_be_fixed_stops_the_plan_and_resume_continues(env):
    s, providers, events, _, tmp = env
    s.settings.planner_profile = "local"                                   # nobody to escalate to
    providers["local"].never = {"b.txt"}
    providers["local"].stream, original = providers["local"].stream, providers["local"].stream
    planned = Planner(plan_json("a.txt", "b.txt", "c.txt"))
    # the planner call goes to the "local" provider too here, so route it by the system prompt
    def route(messages, system, tools, **kw):
        return planned.stream(messages, system, tools, **kw) if system == planner.PLANNER_SYSTEM \
            else original(messages, system, tools, **kw)
    providers["local"].stream = route
    s.run_turn("/delegate make three files")
    assert "Plan stopped: 1/3" in s.messages[-1]["content"] and not (tmp / "c.txt").exists()
    assert "✗ 2. make b.txt" in s.messages[-1]["content"]
    providers["local"].never = set()                                       # fixed the problem; resume
    s.run_turn("/delegate resume")
    assert (tmp / "b.txt").exists() and (tmp / "c.txt").exists()
    assert "Plan complete: 3/3" in s.messages[-1]["content"]


def test_declining_the_plan_runs_a_normal_turn_instead(env, monkeypatch):
    s, providers, events, approvals, tmp = env
    s.on_approve = lambda tool, args: "no"
    monkeypatch.setattr(Session, "_stream_once", lambda self, provider, defs: {"role": "assistant", "content": "ok normally"})
    s.run_turn("/delegate make three files")
    assert not (tmp / "a.txt").exists() and s.messages[-1]["content"] == "ok normally"
    assert any("plan declined" in n for n in notes(events))


def test_plan_approval_can_be_switched_off(env):
    s, providers, events, approvals, tmp = env
    s.settings.delegate_approval = False
    s.run_turn("/delegate make three files")
    assert approvals == [] and (tmp / "c.txt").exists()


def test_auto_approve_skips_both_delegate_questions(env):
    """With auto-approve on, neither "plan this with the strong model?" nor "run this plan?" is asked."""
    s, providers, events, approvals, tmp = env
    s.settings.auto_delegate = "ask"
    s.auto_approve = True
    s.run_turn(BIG)
    assert "delegate" not in [a[0] for a in approvals] and "delegate-plan" not in [a[0] for a in approvals]
    assert (tmp / "a.txt").exists()


def test_planner_unreachable_falls_back_to_the_worker_model(env):
    s, providers, events, _, tmp = env
    providers["cloud"] = Planner(httpx.ConnectError("refused"))
    worker_plan = Planner(plan_json("a.txt"))
    original = providers["local"].stream

    def route(messages, system, tools, **kw):
        return worker_plan.stream(messages, system, tools, **kw) if system == planner.PLANNER_SYSTEM \
            else original(messages, system, tools, **kw)
    providers["local"].stream = route
    s.run_turn("/delegate make a file")
    assert (tmp / "a.txt").exists()
    assert any("could not be reached" in n and "planning with local" in n for n in notes(events))


def test_an_invalid_plan_is_retried_once_with_the_error(env):
    s, providers, events, _, tmp = env
    providers["cloud"] = Planner("sorry, I cannot do JSON", plan_json("a.txt"))
    s.run_turn("/delegate make a file")
    assert providers["cloud"].calls == 2 and (tmp / "a.txt").exists()


def test_no_valid_plan_falls_back_to_a_normal_turn(env, monkeypatch):
    s, providers, events, _, tmp = env
    providers["cloud"] = Planner("nope")
    monkeypatch.setattr(Session, "_stream_once", lambda self, provider, defs: {"role": "assistant", "content": "did it normally"})
    s.run_turn("/delegate make a file")
    assert s.messages[-1]["content"] == "did it normally"
    assert any("could not make a plan" in n for n in notes(events))


def test_cancel_stops_the_plan_between_steps_and_keeps_progress(env):
    s, providers, events, _, tmp = env
    original = s.on_event

    def watch(event, *p, **k):
        original(event, *p, **k)
        if event == "note" and p[0].startswith("✓ step 1/"):
            s.stop()
    s.on_event = watch
    s.run_turn("/delegate make three files")
    assert (tmp / "a.txt").exists() and not (tmp / "c.txt").exists()
    plan = planner.latest_unfinished(str(tmp))
    assert plan and [t.status for t in plan.tasks][0] == "done" and plan.tasks[2].status == "pending"
    last = [e for e in events if e[0] != "model_active"][-1]                 # (the status line is reset after it)
    assert last[0] == "turn_end" and last[-1] == {"aborted": True}


# ---------------------------------------------------------------- automatic check + prefixes

BIG = ("Refactor the permission checks across all the controllers and migrate the tests:\n"
       "1. move the checks into a service\n2. update every controller\n3. rewrite the unit tests")


def test_auto_delegate_off_never_delegates(env, monkeypatch):
    s, providers, events, approvals, tmp = env
    monkeypatch.setattr(Session, "_stream_once", lambda self, provider, defs: {"role": "assistant", "content": "normal"})
    s.run_turn(BIG)
    assert providers["cloud"].calls == 0 and s.messages[-1]["content"] == "normal"


def test_auto_delegate_ask_offers_then_delegates(env):
    s, providers, events, approvals, tmp = env
    s.settings.auto_delegate = "ask"
    providers["cloud"] = Planner(plan_json("a.txt"))
    s.run_turn(BIG)
    assert approvals[0][0] == "delegate" and "large task" in approvals[0][1]["text"]
    assert (tmp / "a.txt").exists()


def test_auto_delegate_ask_can_be_declined_and_solo_skips_the_check(env, monkeypatch):
    s, providers, events, approvals, tmp = env
    s.settings.auto_delegate = "ask"
    monkeypatch.setattr(Session, "_stream_once", lambda self, provider, defs: {"role": "assistant", "content": "normal"})
    s.on_approve = lambda tool, args: (approvals.append(tool) or "no")
    s.run_turn(BIG)
    assert approvals == ["delegate"] and providers["cloud"].calls == 0
    approvals.clear()
    s.run_turn("/solo " + BIG)
    assert approvals == []                                                  # not even asked


def test_delegate_without_a_request_explains_usage(env):
    s, _, events, *_ = env
    s.run_turn("/delegate")
    assert any("usage: /delegate" in n for n in notes(events))


def test_the_worker_and_planner_models_come_from_settings(env):
    s, *_ = env
    assert planner.planner_model(s).name == "cloud" and planner.worker_model(s).name == "local"
    s.settings.planner_profile = ""
    s.settings.fallback_profile = "cloud"
    assert planner.planner_model(s).name == "cloud"                         # falls back to the fallback profile
    s.settings.fallback_profile = ""
    assert planner.planner_model(s).name == s.model_config.name             # then to the current model


def test_new_settings_are_editable_in_the_ui(env):
    from hexiel import settings_edit
    s, *_ = env
    keys = {r["key"] for r in settings_edit.rows(s)}
    assert {"settings.planner_profile", "settings.executor_profile", "settings.auto_delegate",
            "settings.delegate_approval", "settings.delegate_retries"} <= keys
    msg = settings_edit.apply(s, "settings.auto_delegate", "ask", "session")
    assert s.settings.auto_delegate == "ask" and "this session only" in msg
    settings_edit.apply(s, "settings.delegate_retries", "4", "session")
    assert s.settings.delegate_retries == 4


def test_summary_is_a_markdown_list_with_short_details():
    plan = planner.Plan(request="r", summary="do things", tasks=[
        planner.Task(1, "first", "g", status="done", report="Created the thing in the repo root with two functions " * 5,
                     changed=["a.py"]),
        planner.Task(2, "second", "g", status="failed", report="failed: the check failed")])
    text = planner.summary_text(plan, False)
    lines = text.splitlines()
    assert lines[0].startswith("Plan stopped: 1/2") and "/delegate resume" in lines[0]
    bullets = [ln for ln in lines if ln.startswith("- ")]
    assert bullets[0].startswith("- ✓ 1. first — Created") and bullets[0].endswith("…") and len(bullets[0]) < 140
    assert bullets[1].startswith("- ✗ 2. second") and "Files changed: `a.py`" in text


# ---------------------------------------------------------------- which model is working right now

def _track_models(s):
    shown = []
    original = s.on_event

    def watch(event, *p, **k):
        original(event, *p, **k)
        if event == "model_active":
            shown.append(s.active_model)
    s.on_event = watch
    return shown


def test_the_active_model_follows_the_role_that_is_working(env):
    s, providers, events, _, tmp = env
    shown = _track_models(s)
    s.run_turn("/delegate make two files")
    seq = [x for i, x in enumerate(shown) if i == 0 or x != shown[i - 1]]
    assert seq == ["small · gathering context", "big · planning", "small · step 1/3", "small · step 2/3",
                   "small · step 3/3", ""]                      # worker -> planner -> worker per step -> back to normal
    assert s.active_model == ""


def test_the_active_model_shows_the_planner_when_a_step_is_escalated(env):
    s, providers, events, _, tmp = env
    providers["local"].never = {"b.txt"}
    providers["cloud"] = Planner(plan_json("a.txt", "b.txt"))
    shown = _track_models(s)
    s.run_turn("/delegate make two files")
    assert "big · step 2/2, escalated" in shown and shown[-1] == ""


def test_status_displays_use_the_active_model(env):
    from hexiel.webui import status_state
    s, *_ = env
    assert status_state(s)["model_id"] == "small"
    s.set_active_model(PLANNER, "planning")
    assert status_state(s)["model_id"] == "big · planning"
    s.active_model = ""
    assert status_state(s)["model_id"] == "small"


def test_usage_is_tracked_per_model_across_planner_and_worker(env):
    s, providers, *_ = env
    s.run_turn("/delegate make three files")
    big, small = s.usage_by_model["big"], s.usage_by_model["small"]
    assert big == {"requests": 1, "prompt": 500, "cached": 0, "completion": 100}      # the one planning call
    assert small["requests"] >= 7 and small["prompt"] == small["requests"] * 10        # briefing + steps, all folded in


def test_merge_usage_adds_a_helpers_totals():
    mc = ModelConfig(name="t", provider="openai", model="m", base_url="http://x")
    a, b = Session(mc, Settings(), "/tmp", [], "s"), Session(mc, Settings(), "/tmp", [], "s")
    a.record_usage("m", {"prompt_tokens": 10, "completion_tokens": 2})
    b.record_usage("m", {"prompt_tokens": 5, "cached_tokens": 3, "completion_tokens": 1})
    b.record_usage("other", {"prompt_tokens": 7})
    a.merge_usage(b)
    assert a.usage_by_model["m"] == {"requests": 2, "prompt": 15, "cached": 3, "completion": 3}
    assert a.usage_by_model["other"]["prompt"] == 7


def test_a_busy_worker_is_swapped_for_the_fallback_and_back(tmp_path, monkeypatch):
    from hexiel import planner
    from hexiel.config import ModelConfig
    fb = ModelConfig("cloudfast", "openai", "m2", base_url="http://x/v1")
    monkeypatch.setattr(planner, "_config_profile",
                        lambda s, name: {"local": WORKER, "cloudfast": fb}.get(name))
    s = Session(WORKER, Settings(executor_profile="local", worker_fallback_profile="cloudfast"), str(tmp_path), [], "sys")
    notes = []
    s.on_event = lambda e, *p, **k: notes.append(p[0]) if e == "note" else None
    busy = {"v": True}
    monkeypatch.setattr(planner, "_is_busy", lambda mc, st: busy["v"])
    assert planner.choose_worker(s).name == "cloudfast" and "busy" in notes[-1]
    assert planner.choose_worker(s).name == "cloudfast" and len(notes) == 1        # verdict reused, no repeat note
    busy["v"] = False
    s._worker_state["at"] -= 100
    assert planner.choose_worker(s).name == WORKER.name and "free again" in notes[-1]
    s.settings.worker_fallback_profile = ""
    busy["v"] = True
    s._worker_state["at"] -= 100
    assert planner.choose_worker(s).name == WORKER.name                             # no fallback configured: always wait


def test_busy_probe_detects_a_server_that_does_not_answer(monkeypatch):
    import httpx
    from hexiel import planner
    from hexiel.config import ModelConfig
    mc = ModelConfig("l", "openai", "m", base_url="http://x/v1")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(httpx.ReadTimeout("slow")))
    assert planner._is_busy(mc, None)
    class R:
        def raise_for_status(self): pass
    monkeypatch.setattr(httpx, "post", lambda *a, **k: R())
    assert not planner._is_busy(mc, None)
    assert not planner._is_busy(ModelConfig("a", "anthropic", "m"), None)


def _t(i, files, deps=(), status="pending"):
    return planner.Task(id=i, title=f"t{i}", goal="g", files=list(files), depends_on=list(deps), status=status)


def test_independent_steps_run_together_and_clashing_ones_wait():
    plan = planner.Plan("r", "s", [_t(1, ["a.py"]), _t(2, ["b.py"]), _t(3, ["a.py"]), _t(4, [], []), _t(5, ["c.py"], [2])])
    assert [t.id for t in planner.runnable(plan)] == [1, 2]       # 3 shares a.py with 1; 4 names no files so it is a barrier
    plan.tasks[0].status = plan.tasks[1].status = "done"
    assert [t.id for t in planner.runnable(plan)] == [3]          # 4 still behind 3 (earlier, unfinished); 5 behind 4
    plan.tasks[2].status = "done"
    assert [t.id for t in planner.runnable(plan)] == [4]
    plan.tasks[3].status = "done"
    assert [t.id for t in planner.runnable(plan)] == [5]


def test_a_dependency_blocks_a_step_even_when_files_differ():
    plan = planner.Plan("r", "s", [_t(1, ["a.py"]), _t(2, ["b.py"], [1])])
    assert [t.id for t in planner.runnable(plan)] == [1]


def test_worker_count_is_every_core_but_one(monkeypatch):
    import os
    monkeypatch.setattr(os, "cpu_count", lambda: 16)
    assert planner.worker_count(type("S", (), {"settings": Settings()})()) == 15
    assert planner.worker_count(type("S", (), {"settings": Settings(workers=3)})()) == 3
    monkeypatch.setattr(os, "cpu_count", lambda: 1)
    assert planner.worker_count(type("S", (), {"settings": Settings()})()) == 1


def test_a_plan_with_independent_steps_runs_them_in_parallel(tmp_path, monkeypatch):
    import threading, time
    s, *_ = build_plan_session(tmp_path, monkeypatch)
    live, peak = [0], [0]
    lock = threading.Lock()

    def fake(session, plan, task, tools, system):
        with lock:
            live[0] += 1; peak[0] = max(peak[0], live[0])
        time.sleep(0.2)
        with lock:
            live[0] -= 1
        return True
    monkeypatch.setattr(planner, "_run_task", fake)
    plan = planner.Plan("r", "s", [_t(1, ["a"]), _t(2, ["b"]), _t(3, ["c"]), _t(4, ["a"])])
    assert planner.execute(s, plan) and peak[0] == 3 and all(t.status == "done" for t in plan.tasks)


def test_one_failed_step_stops_new_ones_from_starting(tmp_path, monkeypatch):
    s, *_ = build_plan_session(tmp_path, monkeypatch)
    ran = []
    monkeypatch.setattr(planner, "_run_task", lambda se, p, t, tl, sy: (ran.append(t.id), t.id != 1)[1])
    s.settings.workers = 1
    plan = planner.Plan("r", "s", [_t(1, ["a"]), _t(2, ["b"])])
    assert not planner.execute(s, plan) and ran == [1] and plan.tasks[0].status == "failed" and plan.tasks[1].status == "pending"


def build_plan_session(tmp_path, monkeypatch):
    s = Session(WORKER, Settings(planner_profile="", executor_profile="", workers="auto"), str(tmp_path), [], "sys")
    s.save = lambda: None
    s.on_event = lambda *a, **k: None
    monkeypatch.setattr(planner, "save_plan", lambda *a, **k: None)
    monkeypatch.setattr(planner, "sync_todo", lambda *a, **k: None)
    return s, None
