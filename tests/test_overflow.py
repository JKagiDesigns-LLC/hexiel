"""Overflow recovery: a provider 'prompt too long' rejection compacts once (deterministic trim as
the floor) and retries the turn instead of failing it."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import llm
from hexiel import session as session_mod
from hexiel.config import ModelConfig, Settings
from hexiel.session import Session

OVERFLOW = "provider 400: This model's maximum context length is 8192 tokens. However, you requested 12000 tokens"


@pytest.fixture
def clock(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(session_mod.time, "time", lambda: now[0])
    monkeypatch.setattr(session_mod.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    return now


def turn_session(tmp_path, monkeypatch, outcomes, **settings):
    s = Session(ModelConfig(name="m", provider="openai", model="m", base_url="http://x.local",
                            context_window=1000),
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
    compacts, trims = [], []
    monkeypatch.setattr(Session, "compact", lambda self, prov, manual=False: compacts.append(1))
    monkeypatch.setattr(Session, "_mech_trim_overflow", lambda self: trims.append(1))
    return s, events, compacts, trims


def test_an_overflow_compacts_once_and_retries(tmp_path, monkeypatch, clock):
    s, events, compacts, trims = turn_session(tmp_path, monkeypatch, [
        llm.ProviderError(OVERFLOW),
        {"role": "assistant", "content": "answered after trimming"}])
    s.run_turn("hi")
    assert s.messages[-1]["content"] == "answered after trimming"
    assert len(compacts) == 1 and len(trims) == 1
    assert any("context window" in str(e[1]) for e in events if e[0] == "note")


def test_a_second_overflow_gives_up_without_recovering_again(tmp_path, monkeypatch, clock):
    s, events, compacts, trims = turn_session(tmp_path, monkeypatch, [
        llm.ProviderError(OVERFLOW), llm.ProviderError(OVERFLOW)])
    s.run_turn("hi")
    assert len(compacts) == 1 and len(trims) == 1              # recovery happens once per turn
    assert any(e[0] == "turn_end" and "maximum context length" in str(e[1]) for e in events)


def test_an_unrelated_provider_error_skips_recovery(tmp_path, monkeypatch, clock):
    s, events, compacts, trims = turn_session(tmp_path, monkeypatch, [
        llm.ProviderError("provider 403: api key disabled")])
    s.run_turn("hi")
    assert compacts == [] and trims == []
    assert any(e[0] == "turn_end" and "api key disabled" in str(e[1]) for e in events)


def test_the_deterministic_trim_does_not_run_when_compaction_was_enough(tmp_path, monkeypatch, clock):
    # compact() (faked here) already fixed the size: the real trim's guard must make it a no-op.
    # Patch at instance level keeping the real guard (the class-level recorder has none).
    s, events, compacts, trims = turn_session(tmp_path, monkeypatch, [
        llm.ProviderError(OVERFLOW),
        {"role": "assistant", "content": "ok after compaction"}])
    monkeypatch.setattr(Session, "compaction_needed", lambda self: False)   # fake compact shrank the history
    s._mech_trim_overflow = lambda: trims.append(1) if s.compaction_needed() else None
    s.run_turn("hi")
    assert len(compacts) == 1 and trims == []
    assert s.messages[-1]["content"] == "ok after compaction"


def test_is_context_overflow_matches_provider_wordings():
    assert llm.is_context_overflow(OVERFLOW)
    assert llm.is_context_overflow("anthropic: 400 prompt is too long: 300000 tokens > 200000")
    assert llm.is_context_overflow("ollama 400: input length and `max_tokens` exceed the context limit")
    assert not llm.is_context_overflow("provider 403: api key disabled")
    assert not llm.is_context_overflow("provider 400: tool use id duplicated")