"""Cloud tasks (plans, advice, compaction summaries): when the planner/summary model fails or is limited, the cloud fallback
profile answers instead; everything else (digests, triage) is left alone."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import consult, llm, planner
from hexiel.config import ModelConfig, Settings
from tests.test_remote_control import make_session

SONNET = ModelConfig(name="sonnet", provider="anthropic", model="claude-sonnet-5-5", api_key_env="ANTHROPIC_API_KEY")
FLASH = ModelConfig(name="flash", provider="openai", model="glm-flash:cloud", base_url="http://localhost:11434/v1")
LOCAL = ModelConfig(name="local", provider="openai", model="qwen", base_url="http://127.0.0.1:11434/v1")
PROFILES = {"sonnet": SONNET, "flash": FLASH, "local": LOCAL}


def session(tmp_path, monkeypatch, **settings):
    s = make_session(tmp_path)
    s.settings = Settings(planner_profile="sonnet", cloud_fallback_profile="flash", **settings)
    s.model_config = LOCAL
    events = []
    s.on_event = lambda e, *p, **k: events.append((e, *p))
    monkeypatch.setattr(planner, "_config_profile", lambda sess, name: PROFILES.get(name))
    return s, events


def script(monkeypatch, behave):
    """Replace the single-call primitive; behave(model_name) returns (text, usage) or raises."""
    calls = []

    def once(sess, mc, system, user, max_tokens, budget):
        calls.append(mc.name)
        return behave(mc.name)
    monkeypatch.setattr(planner, "_complete_once", once)
    return calls


def limited(name):
    if name == "sonnet":
        raise llm.RateLimitError("429 usage limit", 3600) if _rl_takes_two() else llm.RateLimitError("429 usage limit")
    return "an answer from the fallback", {"prompt_tokens": 10, "completion_tokens": 5}


def _rl_takes_two():
    import inspect
    return len(inspect.signature(llm.RateLimitError.__init__).parameters) > 2


def test_a_limited_planner_hands_the_cloud_task_to_the_fallback(tmp_path, monkeypatch):
    s, events = session(tmp_path, monkeypatch)
    calls = script(monkeypatch, limited)
    text, usage = planner.complete(s, SONNET, "sys", "plan this", cloud=True)
    assert calls == ["sonnet", "flash"] and text == "an answer from the fallback" and usage["_model"] == "flash"
    note = next(e[1] for e in events if e[0] == "note")
    assert "sonnet couldn't be used" in note and "using flash for this cloud task" in note


def test_a_normal_answer_is_untouched_and_names_its_model(tmp_path, monkeypatch):
    s, _ = session(tmp_path, monkeypatch)
    calls = script(monkeypatch, lambda name: ("fine", {"prompt_tokens": 1}))
    text, usage = planner.complete(s, SONNET, "sys", "hi", cloud=True)
    assert calls == ["sonnet"] and usage["_model"] == "sonnet"


def test_only_cloud_tasks_get_the_fallback(tmp_path, monkeypatch):
    s, _ = session(tmp_path, monkeypatch)
    script(monkeypatch, limited)
    with pytest.raises(llm.RateLimitError):
        planner.complete(s, SONNET, "sys", "digest this")                       # cloud=False: a digest, triage, ...


def test_no_fallback_configured_or_same_model_means_the_error_stands(tmp_path, monkeypatch):
    s, _ = session(tmp_path, monkeypatch)
    s.settings = Settings(planner_profile="sonnet")
    script(monkeypatch, limited)
    with pytest.raises(llm.RateLimitError):
        planner.complete(s, SONNET, "sys", "x", cloud=True)
    s.settings = Settings(planner_profile="sonnet", cloud_fallback_profile="sonnet")
    with pytest.raises(llm.RateLimitError):
        planner.complete(s, SONNET, "sys", "x", cloud=True)


def test_ctrl_c_does_not_start_a_fallback_call(tmp_path, monkeypatch):
    s, _ = session(tmp_path, monkeypatch)
    calls = script(monkeypatch, limited)
    s._stop.set()
    with pytest.raises(llm.RateLimitError):
        planner.complete(s, SONNET, "sys", "x", cloud=True)
    assert calls == ["sonnet"]


def test_if_the_fallback_fails_too_its_error_is_raised(tmp_path, monkeypatch):
    s, _ = session(tmp_path, monkeypatch)
    script(monkeypatch, lambda name: (_ for _ in ()).throw(RuntimeError(f"{name} is down")))
    with pytest.raises(RuntimeError, match="flash is down"):
        planner.complete(s, SONNET, "sys", "x", cloud=True)


def test_a_missing_api_key_is_a_failure_not_the_end_of_hexiel(tmp_path, monkeypatch):
    s, _ = session(tmp_path, monkeypatch)
    real = []

    def make(mc, settings):
        if mc.name == "sonnet":
            raise SystemExit("model 'sonnet': set ANTHROPIC_API_KEY")                 # what make_provider does today
        real.append(mc.name)
        return type("P", (), {"stream": staticmethod(lambda *a, **k: iter([("text", "ok"), ("done", {"completion_tokens": 1})]))})()
    monkeypatch.setattr(llm, "make_provider", make)
    monkeypatch.setattr(type(s), "_interruptible", lambda self, stream, provider: stream, raising=False)
    text, usage = planner.complete(s, SONNET, "sys", "x", cloud=True)
    assert text == "ok" and usage["_model"] == "flash" and real == ["flash"]
    s.settings = Settings(planner_profile="sonnet")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):                       # no fallback: a normal, catchable error
        planner.complete(s, SONNET, "sys", "x", cloud=True)


def test_advice_from_the_fallback_says_who_gave_it(tmp_path, monkeypatch):
    s, events = session(tmp_path, monkeypatch)
    script(monkeypatch, limited)
    s.__dict__["_turn_request"] = "make the build pass"
    advice = consult.ask(s, "what now?", "stuck")
    assert advice == "an answer from the fallback"
    assert any(e[0] == "note" and "advice from flash" in e[1] for e in events)


def test_escalation_uses_the_fallback_when_the_planner_cannot_be_started(tmp_path, monkeypatch):
    s, events = session(tmp_path, monkeypatch)

    def make(mc, settings):
        if mc.name == "sonnet":
            raise SystemExit("no key")
        return object()
    monkeypatch.setattr(llm, "make_provider", make)
    assert consult.escalate(s, "stuck", steps=3) is True
    assert s.model_config.name == "flash"
    assert any(e[0] == "note" and "escalating to flash" in e[1] for e in events)
    s2, events2 = session(tmp_path, monkeypatch)
    s2.settings = Settings(planner_profile="sonnet")
    assert consult.escalate(s2, "stuck", steps=3) is False and s2.model_config.name == "local"


def test_compaction_tries_the_summary_model_then_the_cloud_fallback_then_itself(tmp_path, monkeypatch):
    s, _ = session(tmp_path, monkeypatch, compact_profile="sonnet")
    import hexiel.config as config
    monkeypatch.setattr(config, "load_config", lambda *a, **k: type("C", (), {"model": staticmethod(lambda name: PROFILES[name])})())
    monkeypatch.setattr(llm, "make_provider", lambda mc, settings: f"provider:{mc.name}")
    pairs = s._compaction_models("own-provider")
    assert [mc.name for mc, _ in pairs] == ["sonnet", "flash", "local"] and pairs[-1][1] == "own-provider"
    s.settings = Settings(planner_profile="sonnet", compact_profile="sonnet")             # no fallback configured: as before
    assert [mc.name for mc, _ in s._compaction_models("p")] == ["sonnet", "local"]
    s.settings = Settings(planner_profile="sonnet")                                       # no summary model: just the session's own
    assert [mc.name for mc, _ in s._compaction_models("p")] == ["local"]


def test_a_plan_written_by_the_fallback_is_credited_to_it(tmp_path, monkeypatch):
    s, events = session(tmp_path, monkeypatch, executor_profile="local")
    plan_json = json.dumps({"summary": "do it", "tasks": [{"id": 1, "title": "t", "goal": "make the change in a.py", "files": ["a.py"],
                                                          "check": "true", "constraints": "", "depends_on": []}]})
    script(monkeypatch, lambda name: (_ for _ in ()).throw(llm.RateLimitError("limit")) if name == "sonnet"
           else (plan_json, {"prompt_tokens": 50, "completion_tokens": 30}))
    plan = planner.make_plan(s, "add a feature", "brief")
    assert plan.planner == "flash"
    assert any(e[0] == "note" and "plan written by flash" in e[1] for e in events)
