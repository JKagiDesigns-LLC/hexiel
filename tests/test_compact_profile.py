"""Compaction summaries: short, capped, and optionally written by a faster model (compact_profile)."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import llm
from hexiel.config import ModelConfig, Settings
from hexiel.session import COMPACT_MAX_TOKENS, COMPACT_PROMPT, Session

LOCAL = ModelConfig(name="local", provider="openai", model="small", base_url="http://x", max_output_tokens=16384)
CLOUD = ModelConfig(name="cloud", provider="openai", model="big", base_url="http://y", max_output_tokens=16384)


class Writer:
    def __init__(self, text="SUMMARY by %s", error=None):
        self.text, self.error, self.calls = text, error, []

    def stream(self, messages, system, tools, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        yield ("text", self.text % kw["model"] if "%s" in self.text else self.text)
        yield ("done", {})
        yield ("assistant", None)


def setup(tmp_path, monkeypatch, local, cloud=None, profile="cloud", make_error=None):
    providers = {"local": local, "cloud": cloud}
    s = Session(LOCAL, Settings(compact_profile=profile), str(tmp_path), [], "sys")
    s.save = lambda: None
    events, shown = [], []
    s.on_event = lambda e, *p, **k: (events.append((e, *p)), shown.append(s.active_model) if e == "model_active" else None)
    for i in range(6):
        s.messages.append({"role": "user", "content": f"request {i} " + "w" * 300})
        s.messages.append({"role": "assistant", "content": f"did {i}"})

    def fake_make(mc, settings):
        if make_error:
            raise make_error
        return providers[mc.name]
    monkeypatch.setattr(llm, "make_provider", fake_make)
    models = {"cloud": CLOUD, "local": LOCAL}
    monkeypatch.setattr("hexiel.config.load_config", lambda *a, **k: SimpleNamespace(
        model=lambda name: models[name], models=models, default_model="local", settings=Settings()))
    return s, events, shown


def joined(s):
    return "\n".join(m.get("content", "") for m in s.messages)


def test_the_compact_profile_model_writes_the_summary(tmp_path, monkeypatch):
    local, cloud = Writer(), Writer()
    s, events, shown = setup(tmp_path, monkeypatch, local, cloud)
    s.compact(local, manual=True)
    assert cloud.calls and not local.calls and "SUMMARY by big" in joined(s)
    assert cloud.calls[0]["model"] == "big"
    assert "summarizing" in "".join(x for x in shown) and s.active_model == ""      # the status line said so, then reset


def test_summaries_are_capped_in_length(tmp_path, monkeypatch):
    local = Writer()
    s, *_ = setup(tmp_path, monkeypatch, local, profile="")
    s.compact(local, manual=True)
    assert local.calls[0]["max_output_tokens"] == COMPACT_MAX_TOKENS == 3000       # was 8192+: 15 minutes on a local model
    assert "900 words" in COMPACT_PROMPT and "Be dense, not exhaustive" in COMPACT_PROMPT


def test_without_a_compact_profile_the_current_model_is_used(tmp_path, monkeypatch):
    local, cloud = Writer(), Writer()
    s, *_ = setup(tmp_path, monkeypatch, local, cloud, profile="")
    s.compact(local, manual=True)
    assert local.calls and not cloud.calls and "SUMMARY by small" in joined(s)


def test_a_failing_compact_model_falls_back_to_the_current_one(tmp_path, monkeypatch):
    local, cloud = Writer(), Writer(error=llm.RateLimitError("429 usage limit", retry_after=1))
    s, events, _ = setup(tmp_path, monkeypatch, local, cloud)
    s.compact(local, manual=True)
    assert "SUMMARY by small" in joined(s)
    assert any(e[0] == "note" and "could not summarize with cloud" in e[1] and "local" in e[1] for e in events)


def test_a_missing_api_key_falls_back_with_a_note(tmp_path, monkeypatch):
    local = Writer()
    s, events, _ = setup(tmp_path, monkeypatch, local, make_error=SystemExit("model 'cloud': set ANTHROPIC_API_KEY"))
    s.compact(local, manual=True)
    assert "SUMMARY by small" in joined(s)
    assert any(e[0] == "note" and "not usable" in e[1] for e in events)


def test_when_every_model_fails_the_history_is_kept(tmp_path, monkeypatch):
    local = Writer(error=llm.ProviderError("down"))
    s, events, _ = setup(tmp_path, monkeypatch, local, profile="")
    before = list(s.messages)
    s.compact(local, manual=True)
    assert s.messages == before and any(e[0] == "provider_error" for e in events) and not s.compacting


def test_summary_model_is_editable_in_settings(tmp_path, monkeypatch):
    from hexiel import settings_edit
    s, *_ = setup(tmp_path, monkeypatch, Writer(), Writer())
    row = next(r for r in settings_edit.rows(s) if r["key"] == "settings.compact_profile")
    assert row["label"] == "Summary model"
    settings_edit.apply(s, "settings.compact_profile", "cloud", "session")
    assert s.settings.compact_profile == "cloud"
