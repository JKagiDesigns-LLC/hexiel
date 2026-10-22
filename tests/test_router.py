"""The router: a small cloud model sorts each typed message — chat answered by itself, small tasks
for the main model (overflowing to the overflow profile while it is busy), big ones delegated —
and every routed token counts against the daily cap."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import llm, modelbusy, planner, router
from hexiel.config import ModelConfig, Settings, _settings_from
from tests.test_remote_control import make_session

HAIKU = ModelConfig(name="anthropic-haiku", provider="anthropic", model="claude-haiku-5-5",
                    api_key_env="ANTHROPIC_API_KEY")  # pragma: allowlist secret
GMK = ModelConfig(name="gmktec", provider="openai", model="qwen3.6:35b-a3b-coding",
                  base_url="http://127.0.0.1:11434/v1")
PROFILES = {"anthropic-haiku": HAIKU, "gmktec": GMK}
BUDGET = {"prompt_tokens": 400, "completion_tokens": 30}


@pytest.fixture
def env(tmp_path, monkeypatch):
    s = make_session(tmp_path)
    s.model_config = GMK
    s.settings = Settings(router_profile="anthropic-haiku", overflow_profile="anthropic-haiku",
                          overflow_daily_tokens=200000)
    events = []
    s.on_event = lambda e, *p, **k: events.append((e, *p))
    monkeypatch.setattr(planner, "_config_profile", lambda sess, name: PROFILES.get(name))
    monkeypatch.setattr(planner, "_is_busy", lambda mc, settings: False)
    return s, events


def classify_script(monkeypatch, verdict: str, usage=BUDGET):
    """Stub the routing call; verdict = the JSON's "route" value."""
    def once(sess, mc, system, user, max_tokens, budget):
        assert '"route"' in system and max_tokens == 500
        return json.dumps({"route": verdict, "reason": "why not"}), usage
    monkeypatch.setattr(planner, "_complete_once", once)


class Fake:
    """A provider that answers with a plain text reply and remembers what it was asked to serve."""

    def __init__(self, served: list, text="here is my answer"):
        self.served, self.text = served, text

    def stream(self, messages, system, tools, **kw):
        self.served.append({"tools": len(tools), "model": system and True})
        yield ("text", self.text)
        yield ("assistant", {"role": "assistant", "content": self.text})
        yield ("done", {"prompt_tokens": 1500, "completion_tokens": 120})


def serve_with(monkeypatch, served):
    monkeypatch.setattr(llm, "make_provider", lambda mc, settings: Fake(served))


def notes(events):
    return [e[1] for e in events if e[0] == "note"]


def budget():
    path = router._budget_path()
    return json.loads(path.read_text()) if path.exists() else {}


# ---------------------------------------------------------------- classification

def test_a_chat_message_is_answered_by_the_router_and_capped(env, monkeypatch):
    s, events = env
    classify_script(monkeypatch, "chat")
    served = []
    serve_with(monkeypatch, served)
    mc, handled = router.route_turn(s, "thanks, you are a star!", None, False)
    assert handled and served and served[0]["tools"] == 0      # a chat answer carries no tools
    assert s.messages[-1]["content"] == "here is my answer"
    assert any(e[0] == "turn_end" for e in events)
    assert s.model_config is GMK and s.system_prompt == "t"    # restored after the chat turn
    note = " ".join(notes(events))
    assert "answers this directly" in note
    # the chat answer (1500+120) charges the daily counter (the routing call is stubbed above)
    assert budget()["tokens"] == 1500 + 120


def test_record_usage_charges_routed_models(env, monkeypatch):
    s, _ = env
    s.record_usage("claude-haiku-5-5", {"prompt_tokens": 400, "completion_tokens": 30})
    s.record_usage("qwen3.6:35b-a3b-coding", {"prompt_tokens": 999, "completion_tokens": 999})
    assert budget()["tokens"] == 430                            # only the routed model counts


def test_work_messages_run_on_the_main_model_without_delegating(env, monkeypatch):
    s, events = env
    classify_script(monkeypatch, "work")
    served = []
    serve_with(monkeypatch, served)
    mc, handled = router.route_turn(s, "fix the off-by-one in parse_dates.py", None, False)
    assert not handled and mc is GMK
    assert not any("planning it first" in e for e in notes(events))


def test_a_big_request_goes_straight_to_the_plan(env, monkeypatch):
    s, events = env
    classify_script(monkeypatch, "big")
    forced_calls = []
    monkeypatch.setattr(planner, "maybe_delegate",
                        lambda sess, prompt, *, forced=False: forced_calls.append(forced) or True)
    mc, handled = router.route_turn(s, "refactor storage into three modules", None, False)
    assert handled and forced_calls == [True]
    assert any("planning it first" in e for e in notes(events))


def test_an_unreadable_router_reply_counts_as_work(env, monkeypatch):
    s, _ = env
    monkeypatch.setattr(planner, "_complete_once", lambda *a, **k: "I think maybe work?", {})
    served = []
    serve_with(monkeypatch, served)
    mc, handled = router.route_turn(s, "run the tests", None, False)
    assert not handled and mc is GMK


def test_a_broken_router_is_shut_off_once_and_messages_still_run(env, monkeypatch):
    s, events = env

    def once(*a, **k):
        raise RuntimeError("anthropic: 401")
    monkeypatch.setattr(planner, "_complete_once", once)
    assert router.classify(s, "hi") == ("", "router failed")
    assert any("can't be used" in e for e in notes(events))
    assert router.classify(s, "hi again") == ("", "router unusable")     # disabled: no further calls
    served = []
    serve_with(monkeypatch, served)
    mc, handled = router.route_turn(s, "hello", None, False)
    assert not handled and mc is GMK


def test_a_missing_api_key_disables_the_router(env, monkeypatch):
    s, events = env
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    route, why = router.classify(s, "hi")
    assert route == "" and "no api key" in why
    assert any("ANTHROPIC_API_KEY is not set" in e for e in notes(events))


# ---------------------------------------------------------------- overflow

def test_a_small_request_overflows_while_the_main_model_is_busy(env, monkeypatch):
    s, events = env
    classify_script(monkeypatch, "small")
    monkeypatch.setattr(modelbusy, "others", lambda mc, token="": 1)
    served = []
    serve_with(monkeypatch, served)
    mc, handled = router.route_turn(s, "what line ends main.py?", None, False)
    assert not handled and mc is HAIKU
    assert any("gmktec is busy" in e for e in notes(events))


def test_the_full_turn_runs_the_overflow_model_and_restores_the_main(env, monkeypatch):
    s, events = env
    classify_script(monkeypatch, "small")
    monkeypatch.setattr(modelbusy, "others", lambda mc, token="": 1)
    served = []
    serve_with(monkeypatch, served)
    s._turn_body("what line ends main.py?")
    assert served[0]["model"] and s.messages[-1]["content"] == "here is my answer"
    assert s.model_config is GMK                               # the overflow ran this request only
    assert s.messages[0]["content"].startswith("what line ends main.py?")


def test_small_requests_wait_when_the_main_model_is_free(env, monkeypatch):
    s, _ = env
    classify_script(monkeypatch, "small")
    served = []
    serve_with(monkeypatch, served)
    mc, handled = router.route_turn(s, "what line ends main.py?", None, False)
    assert not handled and mc is GMK                          # free model: never overflow


def test_work_never_overflows_even_when_busy(env, monkeypatch):
    s, _ = env
    classify_script(monkeypatch, "work")
    monkeypatch.setattr(modelbusy, "others", lambda mc, token="": 2)
    served = []
    serve_with(monkeypatch, served)
    mc, handled = router.route_turn(s, "fix the off-by-one in parse_dates.py", None, False)
    assert not handled and mc is GMK


def test_the_cap_stops_the_overflow_and_says_so_once(env, monkeypatch):
    s, events = env
    today = router._today()
    router._budget_path().write_text(json.dumps({"date": today, "tokens": 200000, "noted": ""}))
    classify_script(monkeypatch, "small")
    monkeypatch.setattr(modelbusy, "others", lambda mc, token="": 1)
    served = []
    serve_with(monkeypatch, served)
    mc, handled = router.route_turn(s, "peek at one file", None, False)
    assert mc is GMK          # cap spent: the request queues as it always did
    capped = [e for e in notes(events) if "daily cloud cap" in e]
    assert len(capped) == 1


def test_a_new_day_resets_the_counter(env, monkeypatch):
    s, _ = env
    router._budget_path().write_text(json.dumps({"date": "1999-01-01", "tokens": 10 ** 9, "noted": "1999-01-01"}))
    assert router.spent_today() == 0 and not router.over_budget(s)


# ---------------------------------------------------------------- the settings and the gate

def test_settings_round_trip():
    st = _settings_from({"router_profile": "haiku", "overflow_profile": "haiku",
                         "overflow_daily_tokens": "5000"})
    assert (st.router_profile, st.overflow_profile, st.overflow_daily_tokens) == ("haiku", "haiku", 5000)
    assert _settings_from({}).overflow_daily_tokens == 0


def test_internal_and_prefixed_messages_bypass_the_router(env, monkeypatch):
    s, events = env
    calls = []

    def once(*a, **k):
        calls.append(1)
        return json.dumps({"route": "chat", "reason": "x"}), BUDGET
    monkeypatch.setattr(planner, "_complete_once", once)
    s.internal = True
    assert router.route_turn(s, "thanks", None, False) == (GMK, False) and not calls
    s.internal = False
    assert router.route_turn(s, "thanks", "skip", False) == (GMK, False) and not calls


def test_the_old_auto_delegate_gate_still_runs_without_a_router(tmp_path, monkeypatch):
    s = make_session(tmp_path)
    monkeypatch.setattr(planner, "_config_profile", lambda sess, name: PROFILES.get(name))
    asked = []
    monkeypatch.setattr(planner, "maybe_delegate",
                        lambda sess, prompt, *, forced=False: asked.append(forced) or True)
    s.settings = Settings(auto_delegate="auto")               # router off: the plain gate runs
    mc, handled = router.route_turn(s, "refactor everything", None, False)
    assert handled and asked == [False]


def test_charge_targets_are_the_routed_models(env):
    s, _ = env
    assert router.charge_targets(s) == {"claude-haiku-5-5"}
    s.settings = Settings()
    assert router.charge_targets(s) == frozenset()
