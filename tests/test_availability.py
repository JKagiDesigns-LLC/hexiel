"""A cloud account that can't answer (rejected key, no credits, monthly limit, budget stop) is skipped by every
chain for cloud_cooldown_minutes, across processes; the session starts on the next usable model, says so, and
returns by itself when the cooldown runs out."""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import availability, llm, planner, router
from hexiel.config import ModelConfig, Settings
from hexiel.session import cooling_down
from tests.test_fallback import GLM, GMK, SONNET, env  # noqa: F401 - the shared fixture

KIMI = ModelConfig(name="kimi", provider="openai", base_url="https://ollama.com/v1", model="kimi-k2.6")


# ---------------------------------------------------------------- what an error means

@pytest.mark.parametrize("exc,kind", [
    (llm.ProviderError('provider 401: {"error":"unauthorized"}'), "key"),
    (llm.ProviderError("anthropic 401: invalid x-api-key"), "key"),
    (llm.ProviderError("provider 402: payment required"), "credit"),
    (llm.ProviderError("provider 403: this key has no access to that model"), "denied"),
    (llm.RateLimitError('provider 429: you have reached your monthly usage limit, add usage credits', None,
                        long_term=True), "credit"),
    (llm.RateLimitError("provider 429: weekly limit reached", 86400.0, long_term=True), "limit"),
])
def test_account_errors_are_recognised(exc, kind):
    assert availability.classify(exc)[0] == kind


@pytest.mark.parametrize("exc", [
    llm.TransientError("the connection to the model server failed"),
    llm.RateLimitError("provider 429: slow down", 20.0),          # a short limit: the normal wait handles it
    llm.ProviderError("provider 400: bad tool schema"),
    llm.ProviderError("provider 500: internal error"),
    ValueError("not a provider error"),
])
def test_hiccups_and_bad_requests_are_not_account_verdicts(exc):
    assert availability.classify(exc) is None


# ---------------------------------------------------------------- the shared store

def test_one_dead_account_takes_every_profile_on_it_out():
    availability.mark(GLM, "credit", "monthly limit", Settings())
    assert availability.unusable(GLM)["kind"] == "credit"
    assert availability.unusable(KIMI) is not None              # same Ollama account, never asked
    assert availability.unusable(SONNET) is None                # another account
    assert availability.unusable(GMK) is None


def test_local_models_are_never_marked():
    assert availability.mark(GMK, "key", "provider 401", Settings()) is None
    assert availability.unusable(GMK) is None


def test_a_refusal_for_one_model_marks_only_that_profile():
    availability.mark(KIMI, "denied", "provider 403", Settings())
    assert availability.unusable(KIMI) is not None
    assert availability.unusable(GLM) is None


def test_the_cooldown_is_configurable_and_a_shorter_reset_wins():
    entry = availability.mark(SONNET, "key", "provider 401", Settings(cloud_cooldown_minutes=5))
    assert 290 < entry["until"] - time.time() <= 300
    entry = availability.mark(SONNET, "limit", "429", Settings(), seconds=600)
    assert 590 < entry["until"] - time.time() <= 600            # resets in 10 minutes: not an hour
    entry = availability.mark(SONNET, "limit", "429", Settings(), seconds=7 * 86400)
    assert entry["until"] - time.time() <= 3600                 # a weekly reset is re-checked after the default hour


def test_the_default_is_an_hour():
    assert Settings().cloud_cooldown_minutes == 60
    entry = availability.mark(SONNET, "credit", "credit balance too low", Settings())
    assert 3590 < entry["until"] - time.time() <= 3600


def test_expired_verdicts_are_forgotten():
    availability.mark(SONNET, "key", "401", Settings())
    data = availability._read()
    data["anthropic"]["until"] = time.time() - 1
    availability._path().write_text(__import__("json").dumps(data))
    assert availability.unusable(SONNET) is None


def test_other_processes_see_the_verdict(monkeypatch):
    availability.mark(SONNET, "key", "401", Settings())
    availability._cache.update(mtime=None, data={})              # a fresh process: nothing cached
    assert availability.unusable(SONNET) is not None


def test_status_lines_and_recheck(env, monkeypatch):
    s, events = env
    availability.mark(SONNET, "credit", "credit balance too low", Settings())
    text = availability.command(s)
    assert "Anthropic" in text and "out of credits" in text and "/accounts recheck" in text
    monkeypatch.setattr(availability, "_probe_one", lambda mc, st: None)    # every account answers now
    text = availability.command(s, "recheck")
    assert "every cloud account is usable" in text
    assert availability.unusable(SONNET) is None


# ---------------------------------------------------------------- the chains skip it

def test_cooling_down_includes_dead_accounts(env):
    assert not cooling_down("anthropic-sonnet")
    availability.mark(SONNET, "key", "401", Settings())
    assert cooling_down("anthropic-sonnet")
    assert not cooling_down("gmktec")


def test_fallback_walk_skips_a_dead_account(env):
    s, events = env
    s.model_config = GLM                                          # chain: anthropic-sonnet
    GLM2 = ModelConfig(name="glm-flash", provider="openai", base_url="http://localhost:11434/v1",
                       model="glm-5.3-flash:cloud", fallbacks=["anthropic-sonnet", "gmktec"])
    s.model_config = GLM2
    availability.mark(SONNET, "credit", "credit balance too low", Settings())
    assert s._switch_to_fallback("monthly limit") is True
    assert s.model_config.name == "gmktec"


def test_session_starts_on_the_next_usable_model_with_a_notice(env, monkeypatch):
    s, events = env
    GLM2 = ModelConfig(name="glm-flash", provider="openai", base_url="http://localhost:11434/v1",
                       model="glm-5.3-flash:cloud", fallbacks=["anthropic-sonnet", "gmktec"])
    s.model_config = GLM2
    availability.mark(GLM2, "credit", "monthly limit", Settings())
    availability.mark(SONNET, "credit", "credit balance too low", Settings())
    s.save = lambda: None
    s.apply_cloud_check()
    assert s.model_config.name == "gmktec"
    notes = [e[1] for e in events if e[0] == "note"]
    assert any("using gmktec until" in n and "/accounts recheck" in n for n in notes)
    # ...and once the account answers again the session goes back to its own model
    monkeypatch.setattr(planner, "_is_busy", lambda mc, st: False)
    monkeypatch.setattr(planner, "_config_profile", lambda sess, name: {"glm-flash": GLM2, "gmktec": GMK,
                                                                        "anthropic-sonnet": SONNET}.get(name))
    assert router.overflow_or_main(s, "anything", route="work").name == "gmktec"
    availability.clear()
    assert router.overflow_or_main(s, "anything", route="work").name == "glm-flash"


def test_the_start_check_is_reported_once(env, monkeypatch):
    s, events = env
    s.model_config = GMK
    monkeypatch.delenv("HEXIEL_CLOUD_PROBE", raising=False)
    monkeypatch.setattr(availability, "_profiles_in_use", lambda session: [GLM, SONNET, GMK])
    monkeypatch.setattr(availability, "_probe_one", lambda mc, st: llm.RateLimitError(
        "provider 429: you have reached your monthly usage limit", None, long_term=True)
        if mc.name == "glm-flash" else None)
    availability.start(s)
    s.apply_cloud_check()
    s.apply_cloud_check()
    notes = [e[1] for e in events if e[0] == "note" and "☁" in e[1]]
    assert len(notes) == 1 and "Ollama Cloud" in notes[0]
    assert s.model_config.name == "gmktec"                       # a local model: it stays


def test_every_chain_the_session_may_use_is_checked(env, monkeypatch):
    s, events = env
    profiles = {"glm-flash": GLM, "anthropic-sonnet": SONNET, "gmktec": GMK, "kimi": KIMI}
    monkeypatch.setattr(planner, "_config_profile", lambda sess, name: profiles.get(name))
    s.model_config = GMK                                          # fallbacks: anthropic-sonnet, glm-flash
    s.settings = Settings(tool_models={"describe_image": {"models": ["kimi"]}})
    names = [m.name for m in availability._profiles_in_use(s)]
    assert names[0] == "gmktec" and set(names) == {"gmktec", "anthropic-sonnet", "glm-flash", "kimi"}


def test_a_recent_check_is_not_repeated(env, monkeypatch):
    s, events = env
    calls = []
    monkeypatch.setattr(availability, "_profiles_in_use", lambda session: [SONNET])
    monkeypatch.setattr(availability, "_probe_one", lambda mc, st: calls.append(mc.name))
    availability.check_accounts(s)
    availability.check_accounts(s)
    assert calls == ["anthropic-sonnet"]                         # the second session trusts the first one's answer
    availability.check_accounts(s, force=True)
    assert len(calls) == 2


def test_the_budget_stop_needs_no_request(env, monkeypatch):
    s, events = env
    from hexiel import spend
    monkeypatch.setattr(spend, "over_budget", lambda st, account: "ollama is over its budget" if account == "ollama" else "")
    monkeypatch.setattr(availability, "_profiles_in_use", lambda session: [GLM])
    monkeypatch.setattr(availability, "_probe_one", lambda mc, st: pytest.fail("no request for a known budget stop"))
    found = availability.check_accounts(s)
    assert found and found[0][1]["kind"] == "budget"


def test_helper_calls_skip_a_dead_account_without_a_request(env, monkeypatch):
    s, events = env
    availability.mark(SONNET, "key", "401", Settings())
    monkeypatch.setattr(planner, "_complete_once", lambda *a, **k: pytest.fail("no request to a dead account"))
    with pytest.raises(llm.ProviderError, match="skipped until"):
        planner.complete(s, SONNET, "sys", "hi")


def test_a_helper_call_failure_marks_the_account(env, monkeypatch):
    s, events = env

    def refused(*a, **k):
        raise llm.ProviderError('provider 401: {"error":"unauthorized"}')

    monkeypatch.setattr(planner, "_complete_once", refused)
    with pytest.raises(llm.ProviderError):
        planner.complete(s, GLM, "sys", "hi")
    assert availability.unusable(KIMI)["kind"] == "key"


def test_a_rejected_key_fails_over_instead_of_ending_the_turn(env, monkeypatch):
    s, events = env
    s.model_config = SONNET                                       # chain: glm-flash, gmktec

    def stream(provider, tool_defs):
        if s.model_config.name == "anthropic-sonnet":
            raise llm.ProviderError("anthropic 401: invalid x-api-key")
        return {"role": "assistant", "content": f"answered by {s.model_config.name}"}

    monkeypatch.setattr(s, "_stream_once", stream)
    reply = s._stream_assistant(object(), [])
    assert reply["content"] == "answered by glm-flash"
    assert availability.unusable(SONNET)["kind"] == "key"


def test_router_skips_a_dead_router_quietly(env, monkeypatch):
    s, events = env
    s.settings = Settings(router_profile="glm-flash")
    monkeypatch.setattr(planner, "_config_profile", lambda sess, name: {"glm-flash": GLM}.get(name))
    availability.mark(GLM, "credit", "monthly limit", Settings())
    monkeypatch.setattr(planner, "complete", lambda *a, **k: pytest.fail("no request to a dead router"))
    for _ in range(3):
        assert router.classify(s, "hello")[0] == ""
    assert sum("skipped until" in e[1] for e in events if e[0] == "note") == 1


def test_describe_image_skips_a_dead_account(env, monkeypatch):
    s, events = env
    from hexiel import digest
    s.settings = Settings(tool_models={"describe_image": {"models": ["glm-flash", "gmktec"]}})
    monkeypatch.setattr(planner, "_config_profile", lambda sess, name: {"glm-flash": GLM, "gmktec": GMK}.get(name))
    availability.mark(GLM, "credit", "monthly limit", Settings())
    asked = []
    monkeypatch.setattr(planner, "complete", lambda session, mc, *a, **k: (asked.append(mc.name), ("a red button", {}))[1])
    assert digest.describe(s, {"media_type": "image/png", "data": "aGk="}) == "a red button"
    assert asked == ["gmktec"]


# ---------------------------------------------------------------- vision

def test_qwen35_and_36_see_images_but_the_coders_do_not():
    from hexiel.vision import sees_images
    sees = lambda m: sees_images(ModelConfig(name="x", provider="openai", model=m))
    assert sees("qwen3.6:35b-a3b-coding") and sees("qwen3.5:9b-q4_K_M")
    assert not sees("qwen3-coder:30b") and not sees("qwen3:8b") and not sees("qwen3.5-coder:14b")
