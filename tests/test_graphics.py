"""Weather graphics: a weather-shaped question gets a forecast card (shown in the web UI and
the TUI), the raw numbers ride this turn's context so the model narrates real values, and the
card persists in history as a UI-only message that providers never see."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import graphics, llm, planner
from tests.test_remote_control import make_session

CARD = {
    "kind": "weather",
    "place": "Kingston, Massachusetts",
    "unit": "°F", "wind_unit": "mph",
    "current": {"emoji": "⛅", "label": "partly cloudy", "temp": 58, "feels": 54,
                "wind": 9, "humidity": 60},
    "hours": [{"t": "9am", "temp": 52, "pop": 10, "emoji": "⛅"},
              {"t": "12pm", "temp": 57, "pop": 45, "emoji": "🌧️"}],
    "days": [{"d": "today", "hi": 60, "lo": 44, "pop": 40, "emoji": "🌧️", "label": "rain"},
             {"d": "Fri", "hi": 64, "lo": 48, "pop": 5, "emoji": "☀️", "label": "clear"}],
    "updated": "10:18",
}


def test_intent_does_not_spoof_code_questions():
    for yes in ("what's the weather tomorrow", "weather in Boston",
                "how's the weather in Madrid", "should I bring an umbrella",
                "will it rain today", "boston weather", "is it cold outside",
                "forecast for Madrid", "how cold is it outside"):
        assert graphics.is_weather_ask(yes), yes
    for no in ("write me a weather app in python", "fix the forecast bug in my scheduler",
               "how cold should my fridge be for pickles",
               "explain the WEATHER_SYMBOL constant", "how do I parse NWS alert XML?"):
        assert not graphics.is_weather_ask(no), no


def test_maybe_weather_needs_intent_and_a_place(monkeypatch, tmp_path):
    s = make_session(tmp_path)
    assert graphics.maybe_weather(s, "write me a weather app") is None   # regex gate: no model call
    called = []

    def once(sess, mc, system, user, max_tokens, budget):
        called.append(system)
        return json.dumps({"place": ""}), {}

    monkeypatch.setattr(planner, "_complete_once", once)
    assert graphics.maybe_weather(s, "what's the weather?") is None       # model can't name a place
    assert called and '"place"' in called[0]


def test_maybe_weather_returns_a_card(monkeypatch, tmp_path):
    s = make_session(tmp_path)
    monkeypatch.setattr(planner, "_complete_once",
                        lambda *a, **k: ('{"place": "Kingston, MA"}', {}))
    monkeypatch.setattr(graphics, "fetch_weather", lambda place: dict(CARD))
    card = graphics.maybe_weather(s, "what's the weather in my town today")
    assert card and card["place"] == "Kingston, Massachusetts"


def test_card_facts_and_tui_lines_name_the_live_figures():
    facts = graphics.card_facts(CARD)
    assert "Kingston, Massachusetts" in facts and "58°F" in facts
    assert "12pm 57°F (45% rain)" in facts
    lines = graphics.tui_lines(CARD)
    assert any("58°F" in line and "Kingston" in line for line in lines)
    assert any("☔45%" in line for line in lines)      # hours worth mentioning get their pop
    assert any("today: 🌧️ 60°/44°" in line for line in lines)


def test_providers_never_see_graphic_history(tmp_path):
    s = make_session(tmp_path)
    s.messages.append({"role": "user", "content": "weather?"})
    s.messages.append({"role": "graphic", "graphics": dict(CARD)})
    openai_msgs = llm.OpenAICompatProvider._convert_messages(s.messages)
    assert all(m["role"] != "graphic" for m in openai_msgs)
    assert len(openai_msgs) == 1
    ant_msgs = llm.AnthropicProvider._convert_messages(s.messages)
    assert len(ant_msgs) == 1


def test_turn_attaches_the_card_and_restores_the_system_prompt(monkeypatch, tmp_path):
    s = make_session(tmp_path)
    s.system_prompt = "BASE SYSTEM"
    events = []
    s.on_event = lambda e, *p, **k: events.append((e, *p))
    monkeypatch.setattr(graphics, "maybe_weather", lambda sess, text: dict(CARD))

    s._weather_card("what's the weather in kingston")
    assert events and events[0][0] == "graphic"
    assert [m["role"] for m in s.messages][-1] == "graphic"
    assert s.messages[-1]["graphics"]["place"] == "Kingston, Massachusetts"
    assert s.system_prompt.startswith("BASE SYSTEM")
    assert "[weather data for this request]" in s.system_prompt

    s._restore_weather_note()
    assert s.system_prompt == "BASE SYSTEM"           # the note rode that turn only
    assert s._weather_prev_system is None
    s._restore_weather_note()                          # is a no-op afterwards
    assert s.system_prompt == "BASE SYSTEM"


def test_turn_without_a_weather_ask_leaves_everything_alone(tmp_path):
    s = make_session(tmp_path)
    s.system_prompt = "BASE SYSTEM"
    s.on_event = lambda *a, **k: None
    s._weather_card("fix the failing test")            # intent gate inside maybe_weather: no card
    assert s._weather_prev_system is None
    assert s.system_prompt == "BASE SYSTEM"
    assert all(m["role"] != "graphic" for m in s.messages)
