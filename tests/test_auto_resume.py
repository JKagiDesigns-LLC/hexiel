"""Auto-resume on rate/usage limits: reset-time parsing, wait-and-continue,
fallback profile switch, and giving up when a limit won't reset soon."""

import datetime
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from hexiel import llm
from hexiel import session as session_mod
from hexiel.config import ModelConfig, Settings
from hexiel.session import Session


def test_retry_after_parsing():
    r = llm.retry_after_from
    assert r({"retry-after": "30"}, "") == 30
    assert r({"x-ratelimit-reset-requests": "2s", "x-ratelimit-reset-tokens": "6m0s"}, "") == 360
    soon = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5)).isoformat()
    assert 290 < r({"anthropic-ratelimit-input-tokens-reset": soon}, "") <= 300
    assert r({}, "Rate limit reached. Please try again in 1m30s.") == 90
    assert r({}, "something unrelated") is None


def test_status_classification():
    with pytest.raises(llm.RateLimitError) as exc:
        llm.raise_for_status(429, {}, "you have reached your monthly usage limit", "provider")
    assert exc.value.long_term and exc.value.retry_after is None
    with pytest.raises(llm.RateLimitError) as exc:
        llm.raise_for_status(529, {"retry-after": "10"}, "overloaded", "anthropic")
    assert not exc.value.long_term and exc.value.retry_after == 10
    with pytest.raises(llm.ProviderError) as exc:
        llm.raise_for_status(400, {}, "bad request", "provider")
    assert not isinstance(exc.value, llm.RateLimitError)


@pytest.fixture
def clock(monkeypatch):
    """Instant time: sleep advances a fake clock."""
    now = [1_000_000.0]
    monkeypatch.setattr(session_mod.time, "time", lambda: now[0])
    monkeypatch.setattr(session_mod.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    return now


def make(tmp_path, monkeypatch, outcomes, **settings):
    s = Session(ModelConfig(name="cloud", provider="openai", model="m", base_url="http://x"),
                Settings(**settings), str(tmp_path), [], "sys")
    events = []
    s.on_event = lambda e, *p, **k: events.append((e, *p))
    seq = iter(outcomes)

    def fake_once(self, provider, tool_defs):
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item
    monkeypatch.setattr(Session, "_stream_once", fake_once)
    monkeypatch.setattr("hexiel.llm.make_provider", lambda *a, **k: object())
    return s, events


def test_waits_for_reset_then_continues(tmp_path, monkeypatch, clock):
    start = clock[0]
    s, events = make(tmp_path, monkeypatch, [
        llm.RateLimitError("429", retry_after=300),
        {"role": "assistant", "content": "done after the reset"}])
    s.run_turn("hi")
    assert s.messages[-1]["content"] == "done after the reset"
    assert ("rate_limited", 302, "429") in [(e[0], round(e[1]), e[2]) for e in events if e[0] == "rate_limited"]
    assert "resumed" in [e[0] for e in events]
    assert clock[0] - start >= 300


def test_unknown_reset_backs_off_exponentially(tmp_path, monkeypatch, clock):
    s, events = make(tmp_path, monkeypatch, [
        llm.RateLimitError("429"), llm.RateLimitError("429"),
        {"role": "assistant", "content": "ok"}])
    s.run_turn("hi")
    waits = [round(e[1]) for e in events if e[0] == "rate_limited"]
    assert waits == [32, 62] and s.messages[-1]["content"] == "ok"


def test_retry_notes_carry_the_schedule(tmp_path, monkeypatch, clock):
    s, events = make(tmp_path, monkeypatch, [
        llm.RateLimitError("429", retry_after=300),
        {"role": "assistant", "content": "ok"}])
    s.run_turn("hi")
    ev = next(e for e in events if e[0] == "rate_limited")
    assert ev[1] == 302 and ev[3] == 1 and re.match(r"\d\d:\d\d:\d\d", ev[4])   # (wait, reason, attempt, retry-at)


def test_a_small_retry_after_gets_a_backoff_floor(tmp_path, monkeypatch, clock):
    s, events = make(tmp_path, monkeypatch, [
        llm.RateLimitError("429", retry_after=2),
        {"role": "assistant", "content": "ok"}])
    s.run_turn("hi")
    ev = next(e for e in events if e[0] == "rate_limited")                     # floor beats the tight 2 s loop
    assert round(ev[1]) == 62 and ev[3] == 1


def test_long_term_limit_switches_to_fallback(tmp_path, monkeypatch, clock):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[models.cloud]\nprovider="openai"\nmodel="m"\n[models.local]\nprovider="openai"\n'
                   'base_url="http://localhost:11434/v1"\nmodel="qwen"\n')
    monkeypatch.setattr("hexiel.config.CONFIG_FILE", cfg)
    s, events = make(tmp_path, monkeypatch, [
        llm.RateLimitError("monthly usage limit", long_term=True),
        {"role": "assistant", "content": "continued locally"}], fallback_profile="local")
    s.run_turn("hi")
    assert s.model_config.name == "local" and s.messages[-1]["content"] == "continued locally"
    assert ("fallback", "cloud", "local") in [e[:3] for e in events]


def test_gives_up_without_fallback_or_beyond_max_wait(tmp_path, monkeypatch, clock):
    s, events = make(tmp_path, monkeypatch, [llm.RateLimitError("monthly cap", long_term=True)])
    s.run_turn("hi")
    assert any(e[0] == "turn_end" and e[1:] and "monthly cap" in str(e[1]) for e in events)
    s2, events2 = make(tmp_path, monkeypatch, [llm.RateLimitError("429", retry_after=99999)],
                       auto_resume_max_wait=3600)
    s2.run_turn("hi")
    assert not any(e[0] == "rate_limited" for e in events2)


def test_auto_resume_off(tmp_path, monkeypatch, clock):
    s, events = make(tmp_path, monkeypatch, [llm.RateLimitError("429", retry_after=5)], auto_resume=False)
    s.run_turn("hi")
    assert not any(e[0] == "rate_limited" for e in events)
    assert any(e[0] == "turn_end" and "auto-resume is off" in str(e[1:]) for e in events)


# ---------------------------------------------------------------- empty replies

def test_empty_reply_is_nudged_and_never_stored(tmp_path, monkeypatch, clock):
    s, events = make(tmp_path, monkeypatch, [
        {"role": "assistant", "content": ""},                       # one end-of-turn token
        {"role": "assistant", "content": "here is the answer"}])
    s.messages = [{"role": "user", "content": "old"}, {"role": "assistant", "content": ""}]  # a stale empty one
    s.run_turn("continue")
    assert "empty_retry" in [e[0] for e in events]
    assert [m["content"] for m in s.messages] == ["old", "continue", "here is the answer"]


def test_two_empty_replies_report_an_error(tmp_path, monkeypatch, clock):
    s, events = make(tmp_path, monkeypatch, [{"role": "assistant", "content": ""},
                                             {"role": "assistant", "content": "  "}])
    s.run_turn("continue")
    ends = [e for e in events if e[0] == "turn_end"]
    assert ends and "empty reply twice" in str(ends[-1])
    assert all(m["role"] != "assistant" for m in s.messages)


# ---------------------------------------------------------------- dropped connections

REAL_STREAM_ONCE = Session._stream_once        # captured before any test patches it


class _Provider:
    """Streams `items`; an exception instance in the list is raised at that point."""

    def __init__(self, *items):
        self.items = items

    def stream(self, *a, **k):
        for item in self.items:
            if isinstance(item, Exception):
                raise item
            yield item


def test_timeout_before_any_output_is_retried(tmp_path, monkeypatch, clock):
    import httpx
    s, events = make(tmp_path, monkeypatch, [])
    providers = iter([_Provider(httpx.ReadTimeout("timed out")),
                      _Provider(("assistant", {"role": "assistant", "content": "got there"}))])
    monkeypatch.setattr(Session, "_stream_once",
                        lambda self, provider, defs: REAL_STREAM_ONCE(self, next(providers), defs))
    s.run_turn("hi")
    assert s.messages[-1]["content"] == "got there"
    assert any(e[0] == "model_unreachable" for e in events)       # waited a moment, then retried
    assert not any(e[0] == "rate_limited" for e in events)        # a hung server is not a rate limit


def test_repeated_timeouts_give_up_with_a_clear_message(tmp_path, monkeypatch, clock):
    import httpx
    s, events = make(tmp_path, monkeypatch, [])
    calls = []

    def once(self, provider, defs):
        calls.append(1)
        return REAL_STREAM_ONCE(self, _Provider(httpx.ReadTimeout("timed out")), defs)
    monkeypatch.setattr(Session, "_stream_once", once)
    s.run_turn("hi")
    assert len(calls) == session_mod.MAX_TIMEOUT_RETRIES           # not retried forever
    ends = [e for e in events if e[0] == "turn_end"]
    assert ends and "never answered" in str(ends[-1])
    assert not any(e[0] == "rate_limited" for e in events)


def test_refused_connection_keeps_retrying_longer_than_timeouts(tmp_path, monkeypatch, clock):
    import httpx
    s, events = make(tmp_path, monkeypatch, [])
    providers = iter([_Provider(httpx.ConnectError("refused")) for _ in range(4)]
                     + [_Provider(("assistant", {"role": "assistant", "content": "back up"}))])
    monkeypatch.setattr(Session, "_stream_once",
                        lambda self, provider, defs: REAL_STREAM_ONCE(self, next(providers), defs))
    s.run_turn("hi")
    assert s.messages[-1]["content"] == "back up"                  # server restarting: keep waiting


def test_drop_mid_reply_ends_turn_cleanly(tmp_path, monkeypatch, clock):
    import httpx
    s, events = make(tmp_path, monkeypatch, [])
    monkeypatch.setattr(Session, "_stream_once", lambda self, provider, defs: REAL_STREAM_ONCE(
        self, _Provider(("text", "partial ans"), httpx.RemoteProtocolError("peer closed")), defs))
    s.run_turn("hi")                                               # must not raise
    ends = [e for e in events if e[0] == "turn_end"]
    assert ends and "dropped mid-reply" in str(ends[-1])
    assert all(m.get("content") != "partial ans" for m in s.messages)
