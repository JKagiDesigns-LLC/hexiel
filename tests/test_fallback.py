"""Fallback chains: a model that is unreachable, over its limit, or busy with another request
hands the turn to the next profile in its [models.*] `fallbacks` chain (Anthropic first, an
Ollama Cloud model second) instead of failing or hanging."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import llm as lmm
from hexiel import llm, modelbusy, planner, router
from hexiel.config import ModelConfig, Settings
from hexiel.session import cool_down
import hexiel.config as config_module
from tests.test_remote_control import make_session

SONNET = ModelConfig(name="anthropic-sonnet", provider="anthropic", model="claude-sonnet-5-5",
                     api_key_env="ANTHROPIC_API_KEY",   # pragma: allowlist secret
                     fallbacks=["glm-flash", "gmktec"])
GLM = ModelConfig(name="glm-flash", provider="openai", base_url="http://localhost:11434/v1",
                  model="glm-5.3-flash:cloud", fallbacks=["anthropic-sonnet"])
HAIKU = ModelConfig(name="anthropic-haiku", provider="anthropic", model="claude-haiku-5-5",
                    api_key_env="ANTHROPIC_API_KEY")    # pragma: allowlist secret
GMK = ModelConfig(name="gmktec", provider="openai", base_url="http://192.168.3.10:11434/v1",
                  model="qwen3.6:35b-a3b-coding", fallbacks=["anthropic-sonnet", "glm-flash"])
PROFILES = {"anthropic-sonnet": SONNET, "glm-flash": GLM, "anthropic-haiku": HAIKU, "gmktec": GMK}


class FakeConfigFile:
    def __init__(self, profiles=None):
        self.models = dict(profiles or PROFILES)

    def model(self, name):
        if name not in self.models:
            raise KeyError(f"unknown model profile '{name}'")
        return self.models[name]


@pytest.fixture
def env(tmp_path, monkeypatch):
    s = make_session(tmp_path)
    s.model_config = GMK
    s.settings = Settings()
    events = []
    s.on_event = lambda e, *p, **k: events.append((e, *p))
    import hexiel.session as session_module
    session_module._limited_until.clear()          # no cooldowns leaking between tests
    monkeypatch.setattr(config_module, "load_config", lambda *a, **k: FakeConfigFile())
    monkeypatch.setattr(planner, "_config_profile", lambda sess, name: PROFILES.get(name))
    monkeypatch.setattr(lmm, "make_provider", lambda mc, settings: object())   # providers are never streamed here
    monkeypatch.setattr(modelbusy, "others", lambda mc, token="": 0)
    monkeypatch.setattr(router, "over_budget", lambda session: False)
    return s, events


# --------------------------------------------------------------------------- the config key

def test_fallbacks_accept_both_forms():
    single = ModelConfig(name="x", provider="openai", model="m", fallbacks="sonnet")
    assert single.fallbacks == ("sonnet",)
    many = ModelConfig(name="x", provider="openai", model="m", fallbacks=["sonnet", "glm"])
    assert many.fallbacks == ("sonnet", "glm")
    none = ModelConfig(name="x", provider="openai", model="m")
    assert none.fallbacks == ()


# ------------------------------------------------------------------ _switch_to_fallback

def test_switch_walks_the_chain_in_order(env):
    s, events = env
    assert s._switch_to_fallback("monthly limit") is True
    assert s.model_config.name == "anthropic-sonnet"
    assert events[0][0] == "fallback" and events[0][1] == "gmktec"
    assert s._switch_to_fallback("monthly limit") is True      # sonnet's own chain, next link
    assert s.model_config.name == "glm-flash"
    assert s._switch_to_fallback("monthly limit") is False     # every link is tried or current


def test_switch_never_repaints_a_tried_model(env):
    s, _ = env
    s.model_config = SONNET               # sonnet's chain: glm, then back to gmktec
    s.__dict__.pop("_fallback_tried", None)
    assert s._switch_to_fallback("limit") is True
    assert s.model_config.name == "glm-flash"
    assert s._switch_to_fallback("limit") is False      # trying to go back to sonnet is refused


def test_switch_skips_unknown_profile_names(env):
    s, _ = env
    s.model_config = ModelConfig(name="gmktec", provider="openai", base_url="http://127.0.0.1:1/v1",
                                 model="m", fallbacks=["ghost", "glm-flash"])
    assert s._switch_to_fallback("limit") is True
    assert s.model_config.name == "glm-flash"


def test_switch_falls_back_to_fallback_profile(env):
    s, _ = env
    s.model_config = ModelConfig(name="lone", provider="openai", base_url="http://127.0.0.1:1/v1", model="m")
    s.settings = Settings(fallback_profile="glm-flash")
    assert s._switch_to_fallback("limit") is True
    assert s.model_config.name == "glm-flash"


def test_switch_without_any_fallback(env):
    s, _ = env
    s.model_config = ModelConfig(name="lone", provider="openai", base_url="http://127.0.0.1:1/v1", model="m")
    assert s._switch_to_fallback("limit") is False


# ------------------------------------------------- unreachable server -> fail over

def test_stream_fails_over_when_server_unreachable(env, monkeypatch):
    s, events = env

    def boom(provider, tool_defs):
        raise llm.TransientError("the connection to the model server failed (ConnectError)")

    monkeypatch.setattr(s, "_stream_once", boom)
    s._sleep_interruptibly = lambda wait: True
    out = s._stream_assistant(object(), [])
    assert out == {"role": "assistant", "content": ""}
    assert s.model_config.name == "glm-flash"      # gmktec -> sonnet -> glm-flash, then gave up
    assert s._provider_error


def test_stream_settles_on_the_primary_fallback(env, monkeypatch):
    s, events = env
    tries = 0

    def boom(provider, tool_defs):
        nonlocal tries
        tries += 1
        if tries == 1:
            raise llm.RateLimitError("monthly usage limit reached", long_term=True)   # no reset in sight
        return {"role": "assistant", "content": "carrying on"}

    monkeypatch.setattr(s, "_stream_once", boom)
    assert s._stream_assistant(object(), [])["content"] == "carrying on"
    assert s.model_config.name == "anthropic-sonnet"


# ------------------------------------------------------------- busy -> walk the chain

def test_busy_work_spills_to_the_primary_fallback(env, monkeypatch):
    s, events = env
    monkeypatch.setattr(planner, "_is_busy", lambda mc, st: True)
    s.__dict__.pop("_overflow_busy", None)
    assert router.overflow_or_main(s, "refactor the parser", route="work").name == "anthropic-sonnet"
    s.__dict__.pop("_overflow_busy", None)
    s.model_config = ModelConfig(name="edgebox", provider="openai", base_url="http://127.0.0.1:5/v1",
                                 model="m", fallbacks=["glm-flash", "gmktec"])
    assert router.overflow_or_main(s, "refactor the parser", route="work").name == "glm-flash"


def test_ollama_cloud_models_have_no_queue_to_overflow(env, monkeypatch):
    s, events = env
    monkeypatch.setattr(planner, "_is_busy", lambda mc, st: True)
    s.model_config = GLM
    assert router.overflow_or_main(s, "refactor the parser", route="work").name == "glm-flash"


def test_busy_chat_uses_overflow_and_free_main_gets_the_work(env, monkeypatch):
    s, events = env
    monkeypatch.setattr(planner, "_is_busy", lambda mc, st: True)
    s.settings = Settings(overflow_profile="anthropic-haiku", overflow_daily_tokens=200000)
    s.__dict__.pop("_overflow_busy", None)
    assert router.overflow_or_main(s, "thanks!", route="chat").name == "anthropic-haiku"
    monkeypatch.setattr(planner, "_is_busy", lambda mc, st: False)
    s.__dict__.pop("_overflow_busy", None)
    assert router.overflow_or_main(s, "refactor the parser", route="work").name == "gmktec"


def test_limited_overflow_queues_instead_of_a_paid_fallback(env, monkeypatch):
    s, events = env
    monkeypatch.setattr(planner, "_is_busy", lambda mc, st: True)
    s.settings = Settings(overflow_profile="anthropic-haiku")
    cool_down("anthropic-haiku")
    s.__dict__.pop("_overflow_busy", None)
    assert router.overflow_or_main(s, "thanks!", route="chat").name == "gmktec"   # waits for its model


def test_limited_fallback_link_is_skipped_for_a_while(env, monkeypatch):
    s, events = env
    monkeypatch.setattr(planner, "_is_busy", lambda mc, st: True)
    cool_down("anthropic-sonnet")          # sonnet just hit its usage limit
    s.__dict__.pop("_overflow_busy", None)
    assert router.overflow_or_main(s, "refactor the parser", route="work").name == "glm-flash"


def test_failed_primary_recovers_when_its_cooldown_runs_out(env, monkeypatch):
    s, events = env
    monkeypatch.setattr(planner, "_is_busy", lambda mc, st: False)
    s.__dict__.pop("_overflow_busy", None)
    s.__dict__["_failed_over_from"] = "gmktec"     # the session failed over to a fallback earlier
    s.model_config = SONNET
    cool_down("gmktec", 900.0)                     # its limit resets in 15 minutes: keep sonnet
    assert router.overflow_or_main(s, "anything", route="work").name != "gmktec"
    from hexiel.session import _limited_until, FALLBACK_COOLDOWN
    _limited_until["gmktec"] = 0.0                 # ...then the cooldown is spent
    assert router.overflow_or_main(s, "anything", route="work").name == "gmktec"
    assert "_failed_over_from" not in s.__dict__


def test_every_failure_cools_down_the_failing_primary(env, monkeypatch):
    s, env_events = env

    def boom(provider, tool_defs):
        if len(boom.hits) >= 4:
            raise llm.TransientError("still down", timed_out=False, retry_after=0)
        boom.hits.append(1)
        raise llm.TransientError("the connection to the model server failed (ConnectError)")

    boom.hits = []
    monkeypatch.setattr(s, "_stream_once", boom)
    s._sleep_interruptibly = lambda wait: True
    assert s._stream_assistant(object(), []) == {"role": "assistant", "content": ""}
    import hexiel.session as session_module
    assert "gmktec" in session_module._limited_until        # the primary is skipped, not re-probed every turn


def test_busy_work_keeps_its_queue_without_fallbacks(env, monkeypatch):
    s, events = env
    s.model_config = ModelConfig(name="lone", provider="openai", base_url="http://127.0.0.1:1/v1", model="m")
    monkeypatch.setattr(planner, "_is_busy", lambda mc, st: True)
    s.__dict__.pop("_overflow_busy", None)
    assert router.overflow_or_main(s, "refactor the parser", route="work").name == "lone"


def test_cloud_model_never_checks_its_queue(env, monkeypatch):
    s, events = env
    s.model_config = ModelConfig(name="anthropic-sonnet", provider="anthropic", model="claude-sonnet-5-5",
                                 fallbacks=["anthropic-sonnet"])
    monkeypatch.setattr(modelbusy, "is_local", lambda mc: False)

    def no_others(mc, token=""):
        raise AssertionError("the queue was checked for a cloud model")

    monkeypatch.setattr(modelbusy, "others", no_others)
    assert router.overflow_or_main(s, "anything", route="work").name == "anthropic-sonnet"


def test_empty_reply_recovers_unattended_by_failing_over(env, monkeypatch):
    s, events = env
    turn = {}
    assert s._recover_unattended(turn, "the connection dropped mid-reply") is True
    assert s.model_config.name != "gmktec"          # moved on down the chain
    assert turn["recoveries"] == 1


def test_recovery_gives_up_after_the_cap_or_when_auto_resume_off(env):
    import hexiel.session as session_module
    s, events = env
    assert s._recover_unattended({"recoveries": session_module.MAX_UNATTENDED_RECOVERIES}, "x") is False
    s.settings.auto_resume = False
    assert s._recover_unattended({}, "x") is False


def test_recovery_backs_off_when_chain_exhausted(env, monkeypatch):
    s, events = env
    monkeypatch.setattr(s, "_switch_to_fallback", lambda r: False)
    slept = []
    monkeypatch.setattr(s, "_sleep_interruptibly", lambda secs: slept.append(secs) or True)
    assert s._recover_unattended({}, "empty") is True
    assert slept == [30]
