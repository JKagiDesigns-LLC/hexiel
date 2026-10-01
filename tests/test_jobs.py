"""Scheduled jobs: cron matching, due/catch-up logic, locking, delivery."""

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
