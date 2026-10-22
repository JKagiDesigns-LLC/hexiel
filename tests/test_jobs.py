"""Scheduled jobs: cron matching, due/catch-up logic, locking, delivery."""

import re
import time
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from hexiel import jobs as J

D = datetime.datetime


def test_cron_matching():
    mon_8 = D(2026, 10, 5, 8, 0)                       # a Monday
    assert J.cron_matches("0 8 * * 1", mon_8)
    assert J.cron_matches("0 8 * * mon", mon_8)
    assert not J.cron_matches("0 8 * * 2", mon_8)
    assert J.cron_matches("*/15 9-17 * * 1-5", D(2026, 10, 5, 9, 45))
    assert not J.cron_matches("*/15 9-17 * * 1-5", D(2026, 10, 4, 9, 45))   # Sunday
    assert J.cron_matches("@daily", D(2026, 10, 5, 0, 0))
    assert J.cron_matches("0 0 1 * *", D(2026, 11, 1, 0, 0))
    assert J.cron_matches("0 12 * * 0", D(2026, 10, 4, 12, 0)) and J.cron_matches("0 12 * * 7", D(2026, 10, 4, 12, 0))
    with pytest.raises(ValueError):
        J.parse_cron("every monday")


def test_tick_runs_due_jobs_once_and_catches_up(tmp_path):
    job = J.add("weekly report", "0 8 * * 1", "summarise", str(tmp_path))
    created = D.fromtimestamp(job["created"])
    runs = []
    runner = lambda j: (runs.append(j["id"]) or True, "report body")
    nxt = created + datetime.timedelta(days=8)          # at least one Monday 08:00 passed
    assert J.tick(nxt, runner) == [job["id"]]
    assert J.tick(nxt + datetime.timedelta(minutes=1), runner) == []          # not twice
    assert len(runs) == 1
    inbox = list(J.inbox_dir().glob("*weekly-report.md"))
    assert inbox and "report body" in inbox[0].read_text()
    assert J.load()[0]["last_status"] == "ok"


def test_failing_job_does_not_block_others(tmp_path):
    a = J.add("a", "* * * * *", "x", str(tmp_path))
    b = J.add("b", "* * * * *", "y", str(tmp_path))
    def runner(j):
        if j["id"] == a["id"]:
            raise RuntimeError("boom")
        return True, "fine"
    later = D.fromtimestamp(b["created"]) + datetime.timedelta(minutes=2)
    assert set(J.tick(later, runner)) == {a["id"], b["id"]}
    status = {j["id"]: j["last_status"] for j in J.load()}
    assert status == {a["id"]: "failed", b["id"]: "ok"}


def test_lock_prevents_overlap(tmp_path):
    J.add("c", "* * * * *", "z", str(tmp_path))
    lock = J.home_data_dir() / "jobs.lock"
    lock.write_text("123")
    assert J.tick(D.now() + datetime.timedelta(minutes=5), lambda j: (True, "x")) == []


# ---- run_job: a run that fails must say why, never come back blank ----

class FakeSession:
    """Stands in for Session: `script(session)` plays out one headless turn."""

    def __init__(self, script):
        self.script, self.messages = script, []
        self.model_config = type("M", (), {"model": "fake-model"})()
        self.stopped = False
        self.on_event = self.on_approve = None

    def run_turn(self, text):
        self.script(self)

    def stop(self):
        self.stopped = True


def run_with(monkeypatch, script, **limits):
    import hexiel.main as main_mod
    from hexiel import jobs
    sessions = []
    monkeypatch.setattr(main_mod, "build_session", lambda cwd, model: sessions.append(FakeSession(script)) or sessions[0])
    for name, value in limits.items():
        monkeypatch.setattr(jobs, name, value)
    return jobs.run_job({"name": "n", "prompt": "p", "cwd": ".", "access": "readonly"})


def test_a_finished_report_is_ok(monkeypatch):
    def script(s):
        s.on_event("tool_call", {"name": "web_fetch"})
        s.on_event("tool_result", {"name": "web_fetch"}, "page text", False, 0.1)
        s.messages.append({"role": "assistant", "content": "The price is $99."})
        s.on_event("turn_end")
    assert run_with(monkeypatch, script) == (True, "The price is $99.")


def test_a_model_that_never_answers_is_stopped_and_explained(monkeypatch):
    import threading
    release = threading.Event()

    def script(s):
        s.on_event("request", 100)
        while not s.stopped:                       # a request blocked on a silent server
            release.wait(0.05)
    ok, text = run_with(monkeypatch, script, JOB_STALL_SECONDS=0.3, JOB_POLL_SECONDS=0.1)
    assert not ok and "nothing happened" in text and "fake-model" in text and "hung" in text


def test_a_reply_that_errors_reports_the_models_own_reason(monkeypatch):
    def script(s):
        s.on_event("model_unreachable", 5, "connection refused")
        s.on_event("turn_end", {"error": "the model server accepted the request but never answered"})
    ok, text = run_with(monkeypatch, script)
    assert not ok and "never answered" in text and "connection refused" in text


def test_a_finished_turn_with_no_text_is_called_out(monkeypatch):
    def script(s):
        s.on_event("tool_call", {"name": "web_search"})
        s.on_event("tool_result", {"name": "web_search"}, "error: blocked", True, 1.0)
        s.messages.append({"role": "assistant", "content": ""})
        s.on_event("turn_end")
    ok, text = run_with(monkeypatch, script)
    assert not ok and "returned nothing" in text and "web_search" in text and "error: blocked" in text


def test_mid_task_chatter_is_not_mistaken_for_the_report(monkeypatch):
    def script(s):
        s.messages.append({"role": "assistant", "content": "Let me check that page…", "tool_calls": [{"id": "1"}]})
        s.on_event("turn_end", aborted=True)
    ok, text = run_with(monkeypatch, script)
    assert not ok and "did not complete" in text and "Let me check that page" in text


def test_a_crash_and_a_bad_config_are_reported_not_raised(monkeypatch):
    def boom(s):
        raise KeyError("x")
    ok, text = run_with(monkeypatch, boom)
    assert not ok and "crashed: KeyError" in text
    import hexiel.main as main_mod
    from hexiel import jobs

    def bad(cwd, model):
        raise SystemExit("bad config")
    monkeypatch.setattr(main_mod, "build_session", bad)
    ok, text = jobs.run_job({"name": "n", "prompt": "p", "cwd": ".", "model": "nope"})
    assert not ok and "could not start" in text and "bad config" in text


def test_a_report_that_says_the_task_could_not_be_done_is_a_failure(monkeypatch):
    def script(s):
        s.messages.append({"role": "assistant", "content": "COULD NOT COMPLETE: bestbuy.com blocked every request."})
        s.on_event("turn_end")
    ok, text = run_with(monkeypatch, script)
    assert not ok and "could not be completed" in text and "bestbuy.com blocked" in text


def test_progress_is_a_console_style_log_sent_in_batches(monkeypatch):
    sent = []
    import hexiel.main as main_mod
    from hexiel import jobs

    def script(s):
        s.on_event("thinking", "I should fetch ")
        s.on_event("thinking", "the page first")
        s.on_event("text", "Looking for the price. ")
        s.on_event("tool_call", {"name": "web_fetch", "arguments": {"url": "https://example.com/p"}})
        s.on_event("tool_result", {"name": "web_fetch"}, "error: fetch failed: timed out\nsecond line", True, 30.2)
        s.on_event("tool_call", {"name": "web_search", "arguments": {"query": "beelink me pro price"}})
        s.on_event("tool_result", {"name": "web_search"}, "3 results\n1. a\n2. b", False, 1.5)
        s.messages.append({"role": "assistant", "content": "The price is $99."})
        s.on_event("turn_end")
    monkeypatch.setattr(main_mod, "build_session", lambda cwd, model: FakeSession(script))
    real = jobs._Progress
    monkeypatch.setattr(jobs, "_Progress", lambda send: real(send, every=0.0))
    ok, _ = jobs.run_job({"name": "n", "prompt": "p", "cwd": ".", "access": "readonly"}, sent.append)
    log = "".join(sent)
    assert ok and "# running on default (fake-model)" in log and log.count("~ thinking…") == 1
    assert "~ I should fetch the page first" in log                       # a short trace of the thought
    assert "Looking for the price." in log
    assert re.search(r"▸ \[\d+s\] web_fetch https://example.com/p", log)
    assert "✗ 30.2s" in log and "    error: fetch failed: timed out" in log and "    second line" in log
    assert re.search(r"▸ \[\d+s\] web_search beelink me pro price", log) and "✓ 1.5s" in log and "    1. a" in log


def test_progress_is_capped_and_a_broken_sender_never_breaks_the_run():
    from hexiel import jobs
    got = []
    p = jobs._Progress(got.append, every=0.0)
    p.CAP = 50
    for _ in range(20):
        p.event("text", ("x" * 20,))
    assert sum(len(c) for c in got) == 50
    bad = jobs._Progress(lambda t: 1 / 0, every=0.0)
    bad.event("text", ("hello",))           # must not raise


# ---- switching models when one stalls ----

def switching(monkeypatch, scripts, fallbacks):
    """build_session hands out a FakeSession per model name; `scripts` maps model -> script."""
    import hexiel.main as main_mod
    from hexiel import jobs
    asked = []
    monkeypatch.setattr(main_mod, "build_session", lambda cwd, model: asked.append(model) or FakeSession(scripts[model]))
    monkeypatch.setattr(jobs, "_fallbacks", lambda job, tried: fallbacks)
    monkeypatch.setattr(jobs, "JOB_STALL_SECONDS", 0.3)
    monkeypatch.setattr(jobs, "JOB_SWITCH_SECONDS", 0.3)
    monkeypatch.setattr(jobs, "JOB_POLL_SECONDS", 0.1)
    return asked


def stalls(s):
    s.on_event("request", 1)
    while not s.stopped:
        time.sleep(0.02)


def answers(text):
    def script(s):
        s.messages.append({"role": "assistant", "content": text})
        s.on_event("turn_end")
    return script


def test_a_stalled_model_hands_the_job_to_another_one(monkeypatch):
    from hexiel import jobs
    asked = switching(monkeypatch, {None: stalls, "glm-flash": answers("The price is $99.")}, ["glm-flash"])
    log = []
    ok, text = jobs.run_job({"name": "n", "prompt": "p", "cwd": ".", "access": "readonly"}, log.append)
    assert ok and text.startswith("The price is $99.") and "Ran on glm-flash" in text and "nothing happened" in text
    assert asked == [None, "glm-flash"]
    assert "switching to glm-flash" in "".join(log)


def test_a_task_that_cannot_be_done_does_not_burn_through_other_models(monkeypatch):
    from hexiel import jobs
    asked = switching(monkeypatch, {None: answers("COULD NOT COMPLETE: blocked."), "glm-flash": answers("x")}, ["glm-flash"])
    ok, text = jobs.run_job({"name": "n", "prompt": "p", "cwd": ".", "access": "readonly"})
    assert not ok and "blocked" in text and asked == [None]


def test_when_every_model_fails_the_report_lists_each_attempt(monkeypatch):
    from hexiel import jobs
    switching(monkeypatch, {None: stalls, "a": stalls, "b": stalls}, ["a", "b"])
    ok, text = jobs.run_job({"name": "n", "prompt": "p", "cwd": ".", "access": "readonly"})
    assert not ok and "Earlier attempts" in text and "the default model" in text and "a: " in text


def test_fallback_order_prefers_the_ones_you_configured(monkeypatch):
    import hexiel.config as config
    from hexiel import jobs
    cfg = config.Config(default_model="gmktec", models={n: None for n in ("gmktec", "kimi", "glm-flash", "anthropic-sonnet", "opus")},
                        settings=config.Settings(worker_fallback_profile="glm-flash", planner_profile="anthropic-sonnet"))
    monkeypatch.setattr(config, "load_config", lambda *a, **k: cfg)
    assert jobs._fallbacks({}, None) == ["glm-flash", "anthropic-sonnet"]          # explicit ones first, at most two
    assert "gmktec" not in jobs._fallbacks({}, "gmktec")
    assert jobs._fallbacks({}, "glm-flash")[0] == "anthropic-sonnet"


def test_output_followed_by_silence_is_still_sent_promptly():
    """A tool call that starts a 3-minute fetch must show up now, not when something else happens."""
    from hexiel import jobs
    got = []
    p = jobs._Progress(lambda text: got.append((time.time(), text)), every=0.2)
    p.event("text", ("first ",))
    p.event("tool_call", ({"name": "web_fetch", "arguments": {"url": "https://x"}},))     # inside the batching window
    t0 = time.time()
    time.sleep(0.6)
    assert "web_fetch https://x" in "".join(t for _, t in got)
    assert max(when for when, _ in got) - t0 < 0.5
