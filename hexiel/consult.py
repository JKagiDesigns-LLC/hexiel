"""Bring in the strong model when the working one is struggling: advice, and — when it keeps going badly — taking over.

The everyday model (usually local and cheap) does the work. When it hits something hard it can `consult`
the planner model (Sonnet, say): hexiel sends the user's request, the last few steps and the question, and
the answer comes back as advice the working model reads before its next step. If the working model doesn't
ask but is clearly going round in circles (three failing tool calls in a row, or the same call three times),
hexiel asks on its behalf. At most MAX_PER_TURN consultations per turn keeps the cloud bill predictable.

Struggle is a SCORE, not a single rule: failed tool calls, empty or "not found" results, the same call repeated, a very long
grind, and above all the user correcting the model ("no, it IS on the macmini") all add to it. Past ADVICE_AT the strong model
gives advice; past ESCALATE_AT the strong model becomes the working model for a short burst of steps, then control returns
to the cheap model. `/cloud <request>` forces the cloud model for a whole request.
"""

from __future__ import annotations

import json

from . import planner

MAX_PER_TURN = 3
STRUGGLE_ERRORS = 3          # consecutive failing tool calls (an extra push on the score)
STRUGGLE_REPEATS = 3         # the very same call, again and again (an extra push on the score)
CONTEXT_CHARS = 9000

ADVICE_AT = 4.0              # struggle score that gets advice from the strong model
ESCALATE_AT = 7.0            # ... and that moves the WORKING model to the strong model for a burst of steps
ESCALATE_STEPS = 8           # length of a burst (model requests)
MAX_BURSTS_PER_TURN = 2

CONSULT_SYSTEM = (
    "You are a senior engineer advising a coding agent that runs on a smaller model. It will read your advice and then "
    "carry on by itself, so be concrete. You see the user's request, the agent's most recent steps and either its question "
    "or a description of where it is stuck. Reply in under 350 words: what is going wrong (say so plainly if its approach "
    "is mistaken), then the exact next steps — commands to run, files to open, what to change, how to check it worked. "
    "Do not write long code; describe the change precisely instead.")

TOOL_DESCRIPTION = (
    "Ask a stronger model for advice. Use it when you are unsure how to proceed, a fix keeps failing, you must choose "
    "between designs, or a task turns out harder than it looked — before wasting many steps. It costs cloud tokens, "
    f"so ask specific questions (at most {MAX_PER_TURN} per request). The user's request and your recent steps are sent "
    "automatically; put the exact question, and anything not visible in those steps, in `question`.")


def consultant(session):
    """The model that gives advice: the planner profile (else fallback, else the current model)."""
    return planner.planner_model(session)


def unavailable_reason(session) -> str:
    mode = getattr(session.settings, "consult_mode", "auto")
    if mode == "off":
        return "consulting is turned off (consult_mode)"
    if consultant(session).name == session.model_config.name:
        return ("no stronger model is configured — set a Planner model in /config, or you are already on the "
                "strongest one")
    return ""


def _clip(text, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def recent_steps(session, n: int = 12) -> str:
    lines = []
    for m in session.messages[-n:]:
        role = m.get("role")
        if role == "assistant":
            if m.get("content"):
                lines.append(f"[agent] {_clip(m['content'], 500)}")
            for call in m.get("tool_calls") or []:
                lines.append(f"[agent ran {call.get('name')}] {_clip(json.dumps(call.get('arguments', {})), 300)}")
        elif role == "tool":
            flag = " (FAILED)" if m.get("is_error") else ""
            lines.append(f"[result{flag}] {_clip(m.get('content', ''), 600)}")
        elif role == "user":
            lines.append(f"[user] {_clip(m.get('content', ''), 500)}")
    text = "\n".join(lines)
    return text[-CONTEXT_CHARS:]


def used_this_turn(session) -> int:
    return session.__dict__.get("_consults", 0)


def ask(session, question: str, reason: str = "") -> str:
    """One consultation. Returns the advice text; raises on provider problems (callers decide what to show)."""
    cm = consultant(session)
    request = session.__dict__.get("_turn_request", "") or ""
    user = (f"## The user's request\n{_clip(request, 3000)}\n\n## The agent's most recent steps\n{recent_steps(session)}\n\n"
            + (f"## Why you were called\n{reason}\n\n" if reason else "")
            + f"## Question\n{question.strip() or 'The agent is stuck. Diagnose it and say what to do next.'}")
    session.set_active_model(cm, "advising")
    try:
        text, usage = planner.complete(session, cm, CONSULT_SYSTEM, user, max_tokens=900, cloud=True)
    finally:
        session.active_model = ""
        session.on_event("model_active")
    session.__dict__["_consults"] = used_this_turn(session) + 1
    used = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
    session.on_event("note", f"🧭 advice from {usage.get('_model', cm.name)}" + (f" ({used:,} tokens)" if used else ""))
    return text.strip()


def tool_def(session):
    from .tools import ToolDef, text_result

    def run(args, ctx):
        why = unavailable_reason(session)
        if why:
            return text_result(f"consult is not available: {why}", True)
        if used_this_turn(session) >= MAX_PER_TURN:
            return text_result(f"consult limit reached ({MAX_PER_TURN} per request) — continue on your own or ask the user", True)
        try:
            advice = ask(session, str(args.get("question", "")))
        except Exception as exc:  # noqa: BLE001 - a failed consultation is information, not a crash
            return text_result(f"consult failed ({str(exc)[:160] or type(exc).__name__}); continue on your own", True)
        return text_result(f"[advice from {consultant(session).name}]\n{advice}")
    return ToolDef("consult", TOOL_DESCRIPTION,
                   {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
                   readonly=True, always_ok=True, run=run)


# ---------------------------------------------------------------- noticing that the agent is struggling

import re

_CORRECTION = [re.compile(p, re.I) for p in (
    r"\bstop \w+ing\b", r"\bstop (it|that|this|now)\b", r"\bi('m| am)? ?(already |have |'ve )?(told|telling|said|saying|explained)\b",
    r"\blisten( to me)?\b", r"\bpay attention\b", r"\byou('re| are) (wrong|not listening|ignoring|still)\b",
    r"\b(that|this|it)('s| is) (wrong|incorrect|not (right|true|it|what))\b", r"\bnot what i (asked|said|meant|want)",
    r"\bstill (wrong|broken|not|doesn't|isn't|failing)\b", r"\bwhy (are|do|did) you (keep|still|even)\b",
    r"\bkeep(s)? (doing|asking|saying|trying)\b", r"^\s*no\b[,!. ]", r"\bno[,!.]+\s+(it|that|you|i|the|this)\b",
    r"\b(does ?n[o']t|not|isn't) (work|working)\b", r"\bignor(e|ing) (me|what)", r"\bget to work\b", r"^\s*stop\W*$", r"\bi assure you\b", r"!{2,}")]
_EMPHASIS = re.compile(r"\b(NO+|NOT|STOP|NEVER|WRONG|LISTEN|ALREADY|DON'T|DOESN'T|ISN'T|IS|ARE|MUST|ONLY)\b")   # case-sensitive: SHOUTING


def looks_like_correction(text: str) -> bool:
    """True when a user message sounds like correcting or being frustrated with the model (short messages only —
    a pasted log or file is not a correction)."""
    text = (text or "").strip()
    if not text or len(text) > 600:
        return False
    if any(p.search(text) for p in _CORRECTION):
        return True
    return len(_EMPHASIS.findall(text)) >= 2 or bool(re.search(r"\bNO{3,}\b", text))


_NO_INFO = re.compile(r"^\s*(\(no output\)|no such file|not found|no matches?|command not found|cannot access)", re.I)

STOP_ERRORS = 6               # consecutive failing tool calls: the turn ends, state preserved
STOP_REPEATS = 5              # the very same call this many times: the same result, expected different


def hard_stop(session) -> str | None:
    """The runaway budget (Cline's mistake/loop guards as a hard stop): a reason string when the
    turn must end NOW with state preserved — the session stays resumable, nothing was lost."""
    st = _state(session)
    if st["errors"] >= STOP_ERRORS:
        return (f"{STOP_ERRORS} tool calls in a row failed — the turn stopped here. State preserved; "
                "send a new message to resume, or /model to switch models")
    if st["repeat"] >= STOP_REPEATS:
        return (f"the same {st['sig'][0]} call was repeated {STOP_REPEATS} times without getting anywhere"
                " — the turn stopped here. State preserved; send a new message to resume")
    return None


def _fresh_state() -> dict:
    return {"score": 0.0, "errors": 0, "sig": None, "repeat": 0, "steps": 0, "why": [], "advice_step": -99,
            "bursts": 0, "burst": None}


def _state(session) -> dict:
    return session.__dict__.setdefault("_struggle", _fresh_state())


def reset_turn(session) -> None:
    session.__dict__["_consults"] = 0
    session.__dict__["_struggle"] = _fresh_state()


def _add(session, points: float, why: str) -> None:
    st = _state(session)
    st["score"] += points
    st["why"] = (st["why"] + [why])[-4:]


def record_result(session, call: dict, is_error: bool, text: str = "") -> None:
    """Called after every tool call: failures, empty results, repeats and a long grind raise the struggle score."""
    st = _state(session)
    if call.get("name") == "consult":
        return
    st["steps"] += 1
    sig = (call.get("name"), json.dumps(call.get("arguments", {}), sort_keys=True)[:600])
    st["repeat"] = st["repeat"] + 1 if sig == st["sig"] else 1
    st["sig"] = sig
    if is_error:
        st["errors"] += 1
        _add(session, 1.0, "a tool call failed")
        if st["errors"] == STRUGGLE_ERRORS:
            _add(session, 3.0, f"{STRUGGLE_ERRORS} tool calls in a row failed")
    else:
        st["errors"] = 0
        if _NO_INFO.match(text or ""):
            _add(session, 0.4, "several results had nothing in them")     # searching is normal; ten empty ones in a row are not
        else:
            st["score"] = max(0.0, st["score"] - 0.5)                    # real progress cools things down
    if st["repeat"] >= 2:
        _add(session, 1.0, f"the same {sig[0]} call again")
    if st["repeat"] == STRUGGLE_REPEATS:
        _add(session, 3.0, f"the same {sig[0]} call {STRUGGLE_REPEATS} times")
    if st["steps"] >= 20 and st["steps"] % 10 == 0:
        _add(session, 2.0, f"{st['steps']} tool calls in one request")


def note_user_message(session, text: str) -> None:
    """The user spoke to the model mid-conversation: if it sounds like a correction, that is the strongest signal there is."""
    if looks_like_correction(text):
        _add(session, 4.0, "you corrected it")


def struggle_reason(session) -> str:
    return "; ".join(_state(session)["why"][-3:]) or "it is struggling"


# ---------------------------------------------------------------- taking over for a burst of steps

def strong_available(session) -> bool:
    """Is there a different (stronger) model to hand the work to?"""
    return consultant(session).name != session.model_config.name and not getattr(session, "_escalated_from", None)


def can_escalate(session) -> bool:
    """Escalation needs a different (stronger) model than the one working now, and the feature switched on."""
    return (getattr(session.settings, "escalate_mode", "auto") != "off"
            and not getattr(session, "internal", False)
            and consultant(session).name != session.model_config.name
            and not getattr(session, "_escalated_from", None))


def escalate(session, reason: str, steps: int = ESCALATE_STEPS) -> bool:
    """Make the strong model the working model for `steps` requests. False if it can't be done."""
    from . import llm
    cm = consultant(session)
    try:
        provider = llm.make_provider(cm, session.settings)
    except (SystemExit, ValueError, KeyError) as exc:
        alt = planner.cloud_fallback(session, cm)
        try:
            if alt is None or alt.name == session.model_config.name:
                raise ValueError("no cloud fallback")
            provider = llm.make_provider(alt, session.settings)
            session.on_event("note", f"↪ {cm.name} couldn't be used ({str(exc)[:80] or type(exc).__name__}) — escalating to {alt.name}")
            cm = alt
        except (SystemExit, ValueError, KeyError):
            session.on_event("note", f"⚠ could not switch to {cm.name} ({str(exc)[:100] or type(exc).__name__}) — carrying on")
            return False
    st = _state(session)
    st["bursts"] += 1
    st["burst"] = {"left": steps}
    session._escalated_from = session.model_config
    session._escalated_override = getattr(session, "_provider_override", None)   # a failover provider, restored after the burst
    session.model_config = cm
    session._provider_override = provider
    session.set_active_model(cm, "escalated")
    session.on_event("note", f"⬆ switching to {cm.name} " + ("for this request" if steps >= 10 ** 5 else f"for the next {steps} steps")
                     + f" — {reason}")
    return True


def end_burst(session, why: str = "") -> None:
    """Hand the work back to the cheap model."""
    original = getattr(session, "_escalated_from", None)
    if not original:
        return
    session.model_config = original
    session._provider_override = getattr(session, "_escalated_override", None)   # back to what `original` was running on
    session._escalated_override = None
    session._escalated_from = None
    session.active_model = ""
    st = _state(session)
    st["burst"] = None
    st["score"] = 0.0
    st["why"] = []
    session.on_event("model_active")
    session.on_event("note", f"↩ back on {original.model}" + (f" ({why})" if why else ""))


def maybe_help(session) -> None:
    """Called before every model request: during a burst, count it down; otherwise decide whether the struggle score
    calls for advice or for the strong model to take over."""
    st = _state(session)
    burst = st.get("burst")
    if burst:
        burst["left"] -= 1
        if burst["left"] <= 0:
            end_burst(session, "burst finished")
        return
    if st["score"] >= ESCALATE_AT and st["bursts"] < MAX_BURSTS_PER_TURN and can_escalate(session):
        if escalate(session, struggle_reason(session)):
            return
    if (st["score"] >= ADVICE_AT and getattr(session.settings, "consult_mode", "auto") == "auto"
            and not unavailable_reason(session)
            and used_this_turn(session) < MAX_PER_TURN and st["steps"] - st["advice_step"] >= 6):
        reason = struggle_reason(session)
        st["advice_step"] = st["steps"]
        session.on_event("note", f"🧭 {reason} — asking {consultant(session).name} for advice")
        try:
            advice = ask(session, "", reason=reason)
        except Exception as exc:  # noqa: BLE001
            session.on_event("note", f"⚠ could not get advice ({str(exc)[:100] or type(exc).__name__}) — carrying on")
            return
        if advice:
            session.messages.append({"role": "user", "content": (
                f"[Advice from {consultant(session).name}, requested automatically because {reason}. Read it, follow it "
                f"unless you can see it is wrong, and say what you changed.]\n\n{advice}")})
            st.update(errors=0, repeat=0, sig=None)
            st["score"] = 2.0                 # trouble that carries on after advice should escalate soon


maybe_auto = maybe_help          # (the earlier name)
