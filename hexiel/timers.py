"""Countdown timers the agent starts for work that takes a while ("the test suite takes ~30 min").

They live on the session (and in its saved file, so a resumed chat keeps them) and every view shows them: the
terminal footer, the web console header and the phone app, each counting down from the same end time. A timer
that runs out stays up as "over by …" until the agent stops it; one long forgotten drops off by itself."""

import time
import uuid

MAX_TIMERS = 5
MAX_SECONDS = 24 * 3600
STALE_AFTER = 15 * 60          # a finished timer is dropped once it is over by its own length and at least this


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, round(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def live(session) -> list[dict]:
    """The session's timers, minus long-forgotten finished ones."""
    timers = getattr(session, "timers", None)
    if not timers:
        return []
    now = time.time()
    keep = [t for t in timers if now - t["ends"] < max(STALE_AFTER, t["ends"] - t["started"])]
    if len(keep) != len(timers):
        session.timers = keep
    return keep


def describe(timer: dict, now: float | None = None) -> str:
    left = timer["ends"] - (now or time.time())
    when = f"{fmt_duration(left)} left" if left > 0 else f"over by {fmt_duration(-left)}"
    return f"{timer['label']} · {when}"


def summary(session) -> str:
    """One line for a footer: the timer ending soonest, plus how many more."""
    timers = sorted(live(session), key=lambda t: t["ends"])
    if not timers:
        return ""
    return "⏱ " + describe(timers[0]) + (f" (+{len(timers) - 1})" if len(timers) > 1 else "")


def _seconds(args: dict) -> float:
    total = 0.0
    for key, scale in (("hours", 3600), ("minutes", 60), ("seconds", 1)):
        value = args.get(key)
        if value not in (None, ""):
            total += float(value) * scale
    return total


def _find(timers: list[dict], needle: str) -> dict | None:
    needle = needle.strip().lower()
    if not needle:
        return None
    return next((t for t in timers if t["id"] == needle), None) or \
        next((t for t in timers if needle in t["label"].lower()), None)


def run(session, args: dict) -> tuple[str, bool]:
    """The timer tool: (text for the model, is_error)."""
    action = str(args.get("action") or "list").lower()
    timers = list(live(session))
    if action == "start":
        label = " ".join(str(args.get("label") or "").split())[:60]
        if not label:
            return "error: give the timer a label (what you are waiting for, e.g. 'test suite')", True
        try:
            seconds = _seconds(args)
        except (TypeError, ValueError):
            return "error: hours/minutes/seconds must be numbers", True
        if not 0 < seconds <= MAX_SECONDS:
            return "error: the estimate must be more than 0 and at most 24 hours", True
        now = time.time()
        same = next((t for t in timers if t["label"].lower() == label.lower()), None)
        if same is not None:                          # a new estimate for the same wait: keep when it began
            same["ends"] = now + seconds
            text = f"timer '{label}' re-estimated: {fmt_duration(seconds)} from now"
        else:
            if len(timers) >= MAX_TIMERS:
                return f"error: {MAX_TIMERS} timers are already running — stop one first", True
            timers.append({"id": uuid.uuid4().hex[:6], "label": label, "started": now, "ends": now + seconds})
            text = f"timer '{label}' started: {fmt_duration(seconds)}; the user sees it count down"
    elif action == "stop":
        needle = str(args.get("label") or args.get("id") or "")
        found = _find(timers, needle) if needle else (timers[0] if len(timers) == 1 else None)
        if found is None:
            names = ", ".join(t["label"] for t in timers) or "none running"
            return f"error: no timer matches '{needle}' (timers: {names})", True
        timers.remove(found)
        took = fmt_duration(time.time() - found["started"])
        text = f"timer '{found['label']}' stopped after {took}"
    elif action == "clear":
        text = f"cleared {len(timers)} timer(s)"
        timers = []
    elif action == "list":
        return ("\n".join(describe(t) for t in timers) or "no timers running"), False
    else:
        return "error: action must be start, stop, clear or list", True
    session.timers = timers
    session.on_event("timers", timers)
    session.save()
    return text, False


TOOL_DESCRIPTION = (
    "Show the user a countdown for something that takes a while, so they can see how long is left. Use it "
    "whenever you tell the user a wait will be long (e.g. 'the test suite takes about 30 minutes', a build, "
    "a download): action=start with a short label and your estimate (hours/minutes/seconds); start again "
    "with the same label to re-estimate; action=stop (label) as soon as the wait is over — finished or "
    "failed; clear removes all; list shows them.")


def tool_def(session):
    from .tools import ToolDef, text_result

    def tool_run(args, ctx):
        text, bad = run(session, args)
        return text_result(text, bad)
    return ToolDef("timer", TOOL_DESCRIPTION,
                   {"type": "object", "properties": {
                       "action": {"type": "string", "enum": ["start", "stop", "clear", "list"]},
                       "label": {"type": "string", "description": "what is being waited for, e.g. 'test suite'"},
                       "hours": {"type": "number"}, "minutes": {"type": "number"}, "seconds": {"type": "number"}},
                    "required": ["action"]},
                   always_ok=True, run=tool_run)
