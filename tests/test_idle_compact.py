"""Idle compaction aligned to the ~1 h cache TTL: after a turn, a big session schedules a one-shot
compaction for TTL - margin; the timer dies the moment new work starts."""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import llm
from hexiel import session as session_mod
from hexiel.session import CACHE_TTL, IDLE_COMPACT_MARGIN, IDLE_COMPACT_MIN_TOKENS, Session
from hexiel.config import ModelConfig, Settings


@pytest.fixture
def timers(monkeypatch):
    fired = []
    monkeypatch.setattr(threading.Timer, "start", lambda self: fired.append(self))
    return fired


def compacting_session(tmp_path, window=200_000):
    s = Session(ModelConfig(name="m", provider="openai", model="m", base_url="https://cloud.example.com",
                            context_window=window),
                Settings(), str(tmp_path), [], "sys")
    events = []
    s._on_event = lambda e, *p, **k: events.append((e, *p))
    s.mirrors = []
    return s, events


def test_a_big_idle_session_schedules_a_compaction_before_the_ttl(tmp_path, monkeypatch, timers):
    s, _ = compacting_session(tmp_path)
    s.messages = [{"role": "user", "content": "x" * (IDLE_COMPACT_MIN_TOKENS * 3)}]   # big prompt
    s.last_usage = {"prompt_tokens": IDLE_COMPACT_MIN_TOKENS * 2}
    s._usage_msgs = 0
    s._model_activity_at = session_mod.time.time() - 1200                             # idle for 20 min
    s._schedule_idle_compact()
    assert len(timers) == 1
    want = CACHE_TTL - IDLE_COMPACT_MARGIN - 1200
    assert abs(timers[0].interval - want) < 61                                        # fires ~18 min early
    # when the timer fires it compacts with a LIVE provider
    compacted = []
    monkeypatch.setattr(Session, "compact", lambda self, provider, manual=False: compacted.append(provider is not None))
    s._idle_compact(s.__dict__["_idle_token"])
    assert compacted == [True]


def test_new_activity_kills_the_scheduled_compaction(tmp_path, monkeypatch, timers):
    s, _ = compacting_session(tmp_path)
    s.messages = [{"role": "user", "content": "x" * (IDLE_COMPACT_MIN_TOKENS * 3)}]
    s.last_usage = {"prompt_tokens": IDLE_COMPACT_MIN_TOKENS * 2}
    s._usage_msgs = 0
    s._model_activity_at = session_mod.time.time()
    s._schedule_idle_compact()
    token = s.__dict__["_idle_token"]
    compacted = []
    monkeypatch.setattr(Session, "compact", lambda self, provider, manual=False: compacted.append(1))
    s.__dict__["_idle_token"] = "moved on"                # the user sent a new message: the token went stale
    s._idle_compact(token)
    assert compacted == []                                # the timer's work is somebody else's history now


def test_a_small_prompt_never_schedules(tmp_path, monkeypatch, timers):
    s, _ = compacting_session(tmp_path)
    s.messages = [{"role": "user", "content": "short"}]   # re-read is cheaper than a compaction call
    s.last_usage = {"prompt_tokens": 500}
    s._usage_msgs = 0
    s._model_activity_at = session_mod.time.time()
    s._schedule_idle_compact()
    assert timers == []


def test_turn_start_invalidates_and_activity_is_tracked(tmp_path, monkeypatch, timers):
    s, _ = compacting_session(tmp_path)
    monkeypatch.setattr(Session, "_stream_once", lambda self, p, td: {"role": "assistant", "content": "hi"})
    monkeypatch.setattr("hexiel.llm.make_provider", lambda *a, **k: object())
    s.messages = [{"role": "user", "content": "x" * 60_000},
                  {"role": "assistant", "content": "y" * 20_000}]
    s.last_usage = {"prompt_tokens": IDLE_COMPACT_MIN_TOKENS * 3}
    s._usage_msgs = 0
    s.run_turn("hello")
    assert s.__dict__.get("_model_activity_at") is not None
    assert len(timers) == 1 and timers[0].daemon          # a compaction is scheduled for AFTER this turn