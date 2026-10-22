"""The router: a small cloud model sorts each typed message before the main model runs it.

Every request a person types is first classified by the router model (settings.router_profile,
e.g. Claude Haiku — one short call, so a routing decision costs a fraction of a cent):

    chat  greetings, thanks, chit-chat and other questions answerable without tools — answered by
          the router model itself in one short reply (the main model never queues for it)
    small one simple step that needs the project (read one file and answer, one line changed) —
          the main model does it, and it overflows (below) when the main model is busy
    work  real coding work — the main model, exactly as when the router is off
    big   multi-file changes, designs, features, long lists of steps — straight to the
          plan-then-execute path (/delegate)

Overflow (settings.overflow_profile, e.g. Haiku too): while the main model is busy with another
request, chat- and small-sized requests run on the overflow model instead of waiting, and real
work runs for that turn on the main model's fallback chain ([models.*] `fallbacks`, Anthropic
first, an Ollama Cloud model second). A daily token cap (settings.overflow_daily_tokens) counts
every routed token (routing calls, chat answers, overflow turns) and stops the overflow when it
is spent — afterwards requests simply queue again. 0 = no cap.

Failures never break a turn: an unusable router or budget file means the message is handled as
usual. Routing only applies to messages typed by a person — hexiel's own helper sessions (delegated
steps, briefings, subagents) always run on their own model. Config changes reach a running agent
when it restarts, since each process reads its config when it starts.
"""

from __future__ import annotations

import datetime
import json
import os
import threading
import time
from pathlib import Path

from . import llm
from . import modelbusy
from .config import ModelConfig, home_data_dir

ROUTES = ("chat", "small", "work", "big")
BUSY_RECHECK_SECONDS = 45          # how long a "busy" verdict is trusted before probing again

ROUTE_SYSTEM = (
    "You sort messages sent to hexiel, a coding agent the user runs inside their own project. "
    "Reply with ONLY a JSON object: "
    '{"route": "chat" | "small" | "work" | "big", "reason": "<six words max>"}. '
    '"chat" = a greeting, thanks, chit-chat, or any question you can answer from this conversation '
    "and general knowledge alone ('thanks', 'what model are you?') — nothing that needs the "
    "project.\n"
    '"small" = needs the project but is ONE simple step: answer a question about one file, change '
    "one line, run one command or check one thing.\n"
    '"work" = real work a capable model should do directly in the project: debugging, editing code, '
    "running and fixing things.\n"
    '"big" = a design, several files, or a list of steps: plan it first and work through the plan.')

CHAT_NOTE = (
    "\n\nThe router handed the user's newest message to you for a direct answer, without tools: you "
    "cannot read files, run commands or search. Reply briefly, from the conversation and what you "
    "know. If it actually needs the project's files or a command, say so and ask them to send it as "
    "a normal request.")

CHAT_NUDGE = "(Your last reply was empty. Answer the user's newest message directly, and keep it short.)"

_budget_lock = threading.Lock()


# ---------------------------------------------------------------- the profiles

def _profile(session, name: str):
    """A model profile by name, or None when it isn't defined."""
    from . import planner
    return planner._config_profile(session, name)


def _router_mc(session):
    name = getattr(session.settings, "router_profile", "")
    return _profile(session, name) if name else None


# ---------------------------------------------------------------- the entry point

def route_turn(session, prompt: str, delegate_mode: str | None, cloud_turn: bool) -> tuple:
    """Called by session._turn_body before the model work of a turn (the message is already in the
    history). Returns (mc, handled):

    handled True   the turn is over — a chat reply from the router model, or a delegated run.
    handled False  the rest of the turn runs on `mc` — usually the session's own model, but the
                   overflow profile while that one is busy with another request."""
    from . import planner
    if session.internal:                       # helper sessions run on their own model, always
        return session.model_config, False
    if delegate_mode or cloud_turn or session.plan_mode or _router_mc(session) is None:
        # the router stays out: an explicit prefix (/delegate, /solo, /cloud) or it is off —
        # the old auto-delegate gate runs as before, then a plain turn (which may overflow)
        if delegate_mode != "skip":
            if planner.maybe_delegate(session, prompt, forced=delegate_mode == "force"):
                return session.model_config, True
        return overflow_or_main(session, prompt), False

    route, why = classify(session, prompt)
    if route == "chat":
        session.on_event("note", f"🧭 {why} — {route_or_router_name(session)} answers this directly")
        if answer_chat(session, _router_mc(session)):
            return session.model_config, True
        return overflow_or_main(session, prompt), False     # the chat answer failed: run it normally
    if route == "big":
        session.on_event("note", f"🧭 {why} — planning it first")
        if planner.maybe_delegate(session, prompt, forced=True):
            return session.model_config, True
        return session.model_config, False                  # plan declined or unusable: run it normally
    session.on_event("note", f"🧭 {why} — the main model takes it")
    return overflow_or_main(session, prompt, route), False


def route_or_router_name(session) -> str:
    mc = _router_mc(session)
    return mc.name if mc else "the main model"


# ---------------------------------------------------------------- classifying

def classify(session, prompt: str, mc: ModelConfig | None = None) -> tuple[str, str]:
    """(route, reason): 'chat', 'small', 'work' or 'big' — ('', reason) when the router can't be used
    (the caller then treats the message as a normal request)."""
    if mc is None:
        mc = _router_mc(session)
    if mc is None:
        return "", "no router"
    if session.__dict__.get("_router_broken"):
        return "", "router unusable"
    if mc.api_key_env and not os.environ.get(mc.api_key_env):
        session.__dict__["_router_broken"] = True
        session.on_event("note", f"↪ the router is off: {mc.api_key_env} is not set — messages go to the main model")
        return "", "no api key"

    trail = []
    for m in session.messages[-6:]:            # recent turns give 'deploy' messages their context
        c = m.get("content")
        if c:
            trail.append(f"{m.get('role', 'user')}: {str(c)[:300]}")
    user = "\n".join(trail) + f"\n\nThe newest message — the one to sort: {prompt[:2000]}"
    from . import planner
    try:
        # 500 tokens (not less): a tight cap makes the model spend the whole budget on ad-hoc thinking
        # before any text, so the JSON is cut off. At 500 it routes in ~1s on every run.
        text, _ = planner.complete(session, mc, ROUTE_SYSTEM, user, max_tokens=500, budget=20, cloud=True)
    except Exception as exc:  # noqa: BLE001 - a router problem must never break a turn
        session.__dict__["_router_broken"] = True
        first = str(exc).strip().splitlines()[0][:120] if str(exc).strip() else type(exc).__name__
        session.on_event("note", f"↪ the router is off for this session: {mc.name} can't be used "
                                 f"({first}) — messages go to the main model")
        return "", "router failed"
    try:
        verdict = planner._json_from(text)
    except ValueError:
        return "work", "router reply was not JSON"
    route = str(verdict.get("route", "")).strip().lower()
    if route not in ROUTES:
        return "work", "router said something unrecognized"
    return route, str(verdict.get("reason", ""))[:60] or "routed"


# ---------------------------------------------------------------- chat answers

def answer_chat(session, mc) -> bool:
    """Answer the last user entry straight from `mc`, in one tool-less reply. False when it fails
    (the turn then runs on the main model as usual, with the entry already in the history)."""
    prev_mc, prev_system = session.model_config, session.system_prompt
    session.model_config, session.system_prompt = mc, prev_system + CHAT_NOTE
    try:
        try:
            provider = llm.make_provider(mc, session.settings)
        except SystemExit as exc:              # a missing API key and the like
            session.__dict__["_router_broken"] = True
            session.on_event("note", f"↪ the router is off for this session: {exc}")
            return False
        session.set_active_model(mc, "chat")
        assistant = session._stream_assistant(provider, [])   # tools=[]: a reply, not a work turn
        if _empty(assistant):                  # some models end their reply immediately: nudge once
            session.messages.append({"role": "user", "content": CHAT_NUDGE})
            try:
                assistant = session._stream_assistant(provider, [])
            finally:
                session.messages.pop()
        if _empty(assistant):
            return False
        session.messages.append(assistant)
        session.save()
        session.on_event("turn_end")
        return True
    finally:
        session.model_config = prev_mc
        session.system_prompt = prev_system
        session.active_model = ""
        session.on_event("model_active")


def _empty(msg: dict) -> bool:
    return not (msg.get("content") or "").strip() and not msg.get("tool_calls")


# ---------------------------------------------------------------- overflow while the main model is busy

def overflow_or_main(session, prompt: str, route: str = ""):
    """The model for this turn: the session's own, or another model while that one is busy with
    another request. Chat- and small-sized requests run on the overflow profile; real work runs for
    that one turn on the current model's fallback chain ([models.*] `fallbacks` — whatever the
    config declares, e.g. a cloud model first with another cloud model second), then the session
    returns to its own model next turn. With nothing to fall back to, work keeps its place in the
    queue."""
    from . import planner
    st = session.settings
    from .session import cooling_down
    main = session.model_config
    # a failed-over session returns to its primary once the cooldown on it has run out
    boot = _profile(session, getattr(session, "_failed_over_from", ""))
    if boot is not None and boot.name != main.name and not cooling_down(boot.name):
        session.on_event("note", f"✅ {boot.name} is available again — switching back to it")
        session.__dict__.pop("_failed_over_from", None)
        return boot
    if not modelbusy.is_local(main):
        return main                            # cloud models serve everyone at once
    overflow = _profile(session, st.overflow_profile) if st.overflow_profile else None
    if overflow is not None and overflow.name == main.name:
        overflow = None

    state = session.__dict__.setdefault("_overflow_busy", {"busy": False, "at": -1e9})
    now = time.monotonic()
    if modelbusy.others(main) > 0:             # another hexiel window holds it
        state["busy"], state["at"] = True, now
    elif now - state["at"] < BUSY_RECHECK_SECONDS:
        pass                                   # keep the cached verdict briefly
    else:
        state["busy"], state["at"] = planner._is_busy(main, st), now
    if not state["busy"]:
        return main
    if route in ("chat", "small") or (not route and planner.triage_score(prompt)[0] <= 0):
        if overflow is not None and not over_budget(session) and not cooling_down(overflow.name):
            session.on_event("note", f"🔀 {main.name} is busy — this request runs on {overflow.name} instead of waiting")
            return overflow
        return main                            # chat waits for its own model rather than a paid fallback
    # real work: the model's own fallback chain, first link first
    chain = list(main.fallbacks) or ([st.fallback_profile] if st.fallback_profile else [])
    for name in chain:
        if cooling_down(name):                  # limited a few minutes ago: use the next link, try again later
            continue
        fb = _profile(session, name)
        if fb is None or fb.name == main.name:
            continue
        session.on_event("note", f"🔀 {main.name} is busy — this request runs on {fb.name} instead of waiting in the queue")
        return fb
    return main


# ---------------------------------------------------------------- the daily token cap

def _today() -> str:
    return datetime.date.today().isoformat()


def _budget_path() -> Path:
    return home_data_dir() / "router-budget.json"


def _read_locked() -> dict:
    try:
        data = json.loads(_budget_path().read_text())
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    if data.get("date") != _today():           # a new day resets the counter
        data = {"date": _today(), "tokens": 0, "noted": ""}
    data.setdefault("tokens", 0)
    data.setdefault("noted", "")
    return data


def _write_locked(data: dict) -> None:
    path = _budget_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
    except OSError:
        pass                                   # a broken budget file only loses the cap, never a request


def spent_today() -> int:
    with _budget_lock:
        return int(_read_locked()["tokens"])


def charge_daily(tokens: int) -> None:
    """Add a request's tokens (prompt + completion) to today's counter."""
    if tokens <= 0:
        return
    with _budget_lock:
        data = _read_locked()
        data["tokens"] = int(data["tokens"]) + int(tokens)
        _write_locked(data)


def over_budget(session) -> bool:
    """True when the daily cap is spent (and 0 means no cap). Says so once a day."""
    cap = int(getattr(session.settings, "overflow_daily_tokens", 0) or 0)
    if cap <= 0:
        return False
    with _budget_lock:
        data = _read_locked()
        if int(data["tokens"]) < cap:
            return False
        if data.get("noted") != _today():
            data["noted"] = _today()
            _write_locked(data)
            session.on_event("note", f"📵 the daily cloud cap ({cap} tokens) is spent — requests "
                                     "wait for the main model again")
    return True


def charge_targets(session) -> frozenset:
    """The model ids whose usage counts against the daily cap: the router and overflow profiles.
    Session.record_usage looks here every time a request finishes."""
    st = session.settings
    out = set()
    for name in (getattr(st, "router_profile", ""), getattr(st, "overflow_profile", "")):
        mc = _profile(session, name)
        if mc is not None and mc.model:
            out.add(mc.model)
    return frozenset(out)
