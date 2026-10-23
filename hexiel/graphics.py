"""Fun, useful graphics for common questions — starting with weather.

When a message looks like a weather ask, hexiel fetches the forecast from
open-meteo (free, no key), shows a card in the chat (web UI and TUI), and
hides the raw numbers in the turn's context so the model narrates real
values instead of guessing. A full weather card replaces nothing: if the
fetch fails, the turn simply runs as it always did (the model can still
use web search)."""

import re
import time
from datetime import datetime, timedelta, timezone

import httpx

# "is this a weather ask?" — shaped so asking for weather *code* never matches:
# the word "weather" (or forecast/umbrella) plus an asking shape, or a place right before "weather".
WEATHER_WORD_RE = re.compile(
    r"\bweather\b|\bforecast\b|\bumbrella\b|\brain(?:ing)?\b|\bsnow(?:ing)?\b"
    r"|\btemperature\b|\bdegrees\b|°", re.IGNORECASE)
WEATHER_SHAPE_RE = re.compile(
    r"\bwhat(?:'s|’s|\s+is|\s+will|'ll|\s+do(es)?\s+the\s+weather\s+look)\b|\bhows?\s+(?:the\s+)?weather\b"
    r"|\bhow\s+is\s+the\s+weather\b"
    r"|\b(?:like|report|outside|tomorrow|today|tonight|now|right\s+now|this\s+week|next\s+week|"
    r"going\s+to\s+be|gonna\s+be|temperature|degrees|°|raining?|snowing?|hot|cold|sunny|cloudy|"
    r"foggy|humid|umbrella)\b", re.IGNORECASE)
WEATHER_PLACE_RE = re.compile(
    r"\bweather\s+(?:in|at|for)\s+[A-Za-z]"
    r"|\bforecast\s+(?:for|in|at)\s+[A-Za-z]"
    r"|(?:\b\w[\w,.'’\-]{0,28}\s+)?\bweather\b\s*(?:\?|\s*$)", re.IGNORECASE)
# asks that never say the word "weather"
WEATHER_STANDALONE_RE = re.compile(
    r"\bhow\s+(?:hot|cold|warm|sunny|rainy|humid|windy)\s+(?:is\s+it|will\s+it\s+be)\b"
    r"|\b(?:did\s+it|when\s+did\s+it)\s+(?:rain|snow|hail|sleet|raining|snowing)\b"
    r"|\b(?:is|was)\s+it\s+(?:hot|cold|warm|sunny|humid|windy|snowing|raining)\b"
    r"|\b(?:will\s+it\s+be|do\s+i\s+need)\s+(?:hot|cold|sunny)?\s?(?:raincoat|jacket|umbrella)\b",
    re.IGNORECASE)


def is_weather_ask(text: str) -> bool:
    t = text or ""
    if WEATHER_PLACE_RE.search(t) or WEATHER_STANDALONE_RE.search(t):
        return True
    return bool(WEATHER_WORD_RE.search(t) and WEATHER_SHAPE_RE.search(t))

_GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

_EMOJI = {0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️", 45: "🌫️", 48: "🌫️",
          51: "🌦️", 53: "🌦️", 55: "🌦️", 61: "🌧️", 63: "🌧️", 65: "🌧️",
          66: "🌧️", 67: "🌧️", 71: "🌨️", 73: "🌨️", 75: "❄️", 77: "❄️",
          80: "🌦️", 81: "🌧️", 82: "🌧️", 85: "🌨️", 86: "❄️",
          95: "⛈️", 96: "⛈️", 99: "⛈️"}
_LABEL = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
          45: "foggy", 48: "rime fog", 51: "light drizzle", 53: "drizzle",
          55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
          66: "freezing rain", 67: "freezing rain", 71: "light snow",
          73: "snow", 75: "heavy snow", 77: "snow grains",
          80: "light showers", 81: "showers", 82: "heavy showers",
          85: "snow showers", 86: "snow showers", 95: "thunderstorm",
          96: "thunderstorm w/ hail", 99: "thunderstorm w/ hail"}


def weather_emoji(code: int) -> str:
    return _EMOJI.get(int(code or 0), "🌡️")


def weather_label(code: int) -> str:
    return _LABEL.get(int(code or 0), "unknown")


def _loc_from_model(session, text: str) -> str:
    """The place the message means, from the router model (it reads the whole
    sentence and conversation). Empty string when it can't tell."""
    from . import planner
    mc = None
    from . import router
    mc = router._router_mc(session) or session.model_config
    system = ('Reply with ONLY {"place": "<city, region>"}. The user asks about the weather; '
              "give the place their newest message refers to — from the message itself, otherwise "
              "the place the conversation has been about. If you truly cannot tell, reply "
              '{"place": ""}.')
    try:
        text_out, _ = planner.complete(session, mc, system, text[:1500],
                                       max_tokens=500, budget=20, cloud=True)
        return str(planner._json_from(text_out).get("place", "") or "").strip()
    except Exception:      # noqa: BLE001 - extraction failure just means no card
        return ""


def _http(url: str, params: dict) -> dict | None:
    try:
        r = httpx.get(url, params=params, timeout=6,
                      headers={"User-Agent": "hexiel/0.4 weather card"})
        r.raise_for_status()
        return r.json()
    except Exception:      # noqa: BLE001 - a dead network must never break a turn
        return None


def fetch_weather(place: str) -> dict | None:
    """A structured weather card for `place`, or None (no network, unknown place)."""
    geo = _http(_GEO_URL, {"name": place, "count": 1, "language": "en", "format": "json"})
    hit = (geo or {}).get("results") or []
    if not hit:
        return None
    spot = hit[0]
    lat, lon = spot["latitude"], spot["longitude"]
    imperial = spot.get("country_code") in ("US", "LR", "MM")
    unit = "fahrenheit" if imperial else "celsius"
    wind_unit = "mph" if imperial else "km/h"
    now = datetime.now(timezone.utc)
    data = _http(_FORECAST_URL, {
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
        "hourly": "temperature_2m,precipitation_probability,weather_code",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
        "temperature_unit": unit, "wind_speed_unit": wind_unit,
        "forecast_days": "4", "timezone": "auto",
    })
    if not data:
        return None

    unit_s = "°F" if imperial else "°C"
    cur = data.get("current") or {}
    hourly = data.get("hourly") or {}
    daily = data.get("daily") or {}
    now_local = (datetime.now(timezone.utc)
                 + timedelta(seconds=data.get("utc_offset_seconds", 0))).replace(tzinfo=None)

    # the next 8 hours from "now", as the location's own clock reads it
    hours, stamps = [], hourly.get("time") or []
    temps, pops = hourly.get("temperature_2m") or [], hourly.get("precipitation_probability") or []
    codes = hourly.get("weather_code") or []
    for idx, stamp in enumerate(stamps):
        try:
            t = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if t < now_local - timedelta(minutes=90):
            continue
        hh = (t.hour % 12) or 12
        label = f"{hh}{'am' if t.hour < 12 else 'pm'}"
        hours.append({"t": label, "temp": round(temps[idx]), "pop": pops[idx],
                      "emoji": weather_emoji(codes[idx])})
        if len(hours) >= 8:
            break

    days = []
    for idx, stamp in enumerate(daily.get("time") or []):
        d = datetime.fromisoformat(stamp).date()
        name = "today" if d == now.date() else d.strftime("%a")
        days.append({"d": name, "hi": round(daily["temperature_2m_max"][idx]),
                     "lo": round(daily["temperature_2m_min"][idx]),
                     "pop": (daily.get("precipitation_probability_max") or [0] * len(daily["time"]))[idx],
                     "emoji": weather_emoji(daily["weather_code"][idx]),
                     "label": weather_label(daily["weather_code"][idx])})

    code = int(cur.get("weather_code") or 0)
    card = {
        "kind": "weather",
        "place": f"{spot['name']}, {spot.get('admin1', '')}".rstrip(", "),
        "unit": unit_s,
        "wind_unit": "mph" if imperial else "km/h",
        "current": {
            "emoji": weather_emoji(code), "label": weather_label(code),
            "temp": round(cur.get("temperature_2m", 0)),
            "feels": round(cur.get("apparent_temperature", 0)),
            "wind": round(cur.get("wind_speed_10m", 0)),
            "humidity": round(cur.get("relative_humidity_2m", 0)),
        },
        "hours": hours, "days": days,
        "updated": now_local.strftime("%H:%M"),
    }
    return card


def _summary_line(card: dict) -> str:
    c = card["current"]
    u = card["unit"]
    days = " | ".join(f"{d['d']} {d['emoji']} hi {d['hi']}{u} lo {d['lo']}{u}"
                      + (f" pop {d['pop']}%" if d["pop"] else "") for d in card["days"])
    return (f"Weather for {card['place']} (updated {card['updated']}): currently {c['emoji']} "
            f"{c['temp']}{u}, feels like {c['feels']}{u}, {c['label']}, wind {c['wind']}, "
            f"humidity {c['humidity']}%. Hours: " +
            ", ".join(f"{h['t']} {h['temp']}{u}" + (f" ({h['pop']}% rain)" if h["pop"] else "")
                      for h in card["hours"]) +
            ". Days: " + days)


def maybe_weather(session, user_text: str) -> dict | None:
    """A weather card for the turn, or None. Only messages that read like a weather ask are
    considered; every failure path silently returns None so the turn is never delayed long."""
    if not is_weather_ask(user_text):
        return None
    place = _loc_from_model(session, user_text)
    if not place:
        return None
    started = time.time()
    card = fetch_weather(place)
    if not card or time.time() - started > 12:      # too slow to be a worthwhile wait
        return None
    return card


def card_facts(card: dict) -> str:
    """The facts the model narrates — attached to this turn's context only."""
    return _summary_line(card)


def tui_lines(card: dict) -> list[str]:
    """The TUI's rendering of a weather card, as text lines."""
    c, u = card["current"], card["unit"]
    lines = [f"{c['emoji']} {card['place']} — {c['temp']}{u} (feels {c['feels']}{u}), "
             f"{c['label']}, wind {c['wind']}, humidity {c['humidity']}%"]
    if card.get("hours"):
        lines.append(" · ".join(f"{h['t']} {h['temp']}{u}"
                                + (f" ☔{h['pop']}%" if h["pop"] >= 30 else "") for h in card["hours"][:6]))
    if card.get("days"):
        lines.append(" · ".join(f"{d['d']}: {d['emoji']} {d['hi']}°/{d['lo']}°"
                                + (f" ☔{d['pop']}%" if d["pop"] >= 30 else "") for d in card["days"][:4]))
    return lines
