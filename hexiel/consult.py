"""Ask the strong model for advice — as a tool the working model can call, and automatically when it is visibly stuck.

The everyday model (usually local and cheap) does the work. When it hits something hard it can `consult`
the planner model (Sonnet, say): hexiel sends the user's request, the last few steps and the question, and
the answer comes back as advice the working model reads before its next step. If the working model doesn't
ask but is clearly going round in circles (three failing tool calls in a row, or the same call three times),
hexiel asks on its behalf. At most MAX_PER_TURN consultations per turn keeps the cloud bill predictable.
"""

from __future__ import annotations

import json

from . import planner

MAX_PER_TURN = 3
STRUGGLE_ERRORS = 3          # consecutive failing tool calls
STRUGGLE_REPEATS = 3         # the very same call, again and again
CONTEXT_CHARS = 9000

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
        text, usage = planner.complete(session, cm, CONSULT_SYSTEM, user, max_tokens=900)
    finally:
        session.active_model = ""
        session.on_event("model_active")
    session.__dict__["_consults"] = used_this_turn(session) + 1
    used = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
    session.on_event("note", f"🧭 advice from {cm.name}" + (f" ({used:,} tokens)" if used else ""))
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


# ---------------------------------------------------------------- noticing that the agent is stuck

def reset_turn(session) -> None:
    session.__dict__["_consults"] = 0
    session.__dict__["_struggle"] = {"errors": 0, "sig": None, "repeat": 0, "asked": False}


def record_result(session, call: dict, is_error: bool) -> None:
    """Called after every tool call: tracks failures in a row and identical repeated calls."""
    st = session.__dict__.setdefault("_struggle", {"errors": 0, "sig": None, "repeat": 0, "asked": False})
    if call.get("name") == "consult":
        return
    sig = (call.get("name"), json.dumps(call.get("arguments", {}), sort_keys=True)[:600])
    st["repeat"] = st["repeat"] + 1 if sig == st["sig"] else 1
    st["sig"] = sig
    st["errors"] = st["errors"] + 1 if is_error else 0


def struggle_reason(session) -> str:
    st = session.__dict__.get("_struggle")
    if not st or st.get("asked"):
        return ""
    if st["errors"] >= STRUGGLE_ERRORS:
        return f"the last {st['errors']} tool calls in a row failed"
    if st["repeat"] >= STRUGGLE_REPEATS:
        return f"the same {st['sig'][0]} call was repeated {st['repeat']} times"
    return ""


def maybe_auto(session) -> None:
    """Called before each model request. If the agent is stuck, get advice for it and put it in front of it."""
    if getattr(session.settings, "consult_mode", "auto") != "auto" or unavailable_reason(session):
        return
    if used_this_turn(session) >= MAX_PER_TURN:
        return
    reason = struggle_reason(session)
    if not reason:
        return
    st = session.__dict__["_struggle"]
    st["asked"] = True                         # once per stuck spell; a new streak of failures can ask again
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
        st.update(errors=0, repeat=0, sig=None, asked=False)
