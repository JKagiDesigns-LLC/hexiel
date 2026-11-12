"""Cloud accounts that can't answer right now — a rejected key, no credits left, a monthly limit, the budget
stop — are remembered for `cloud_cooldown_minutes` (default 60) so no chain keeps hammering them: the main
model's fallbacks, the router, overflow, planner/summary models, describe_image and the tool models all skip
them, in every hexiel process (the verdicts live in one small file next to the spend ledger).

A session checks the cloud accounts it may use once when it starts (in the background, one tiny request
per account, at most every PROBE_FRESH_SECONDS across processes). When its own model is on an unusable
account it starts on the next usable model of its fallback chain — usually the local one — says so, and
switches back by itself when the cooldown runs out (router.overflow_or_main). `/accounts` shows the state,
`/accounts recheck` forgets it and checks again.

Keys: the billing account (spend.account_for: "ollama", "anthropic", "openai", a host) — one dead account
takes every profile on it out — or "profile:NAME" for a refusal that concerns one model only (403)."""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time

from .config import home_data_dir

DEFAULT_MINUTES = 60
PROBE_FRESH_SECONDS = 600          # a usable verdict this recent is trusted: no new probe at session start
PROBE_TIMEOUT = 20.0               # one tiny request per account; a slower answer is not a verdict either way
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "mtime": None, "data": {}}

KIND_TEXT = {
    "key": "the API key was rejected",
    "credit": "out of credits / usage limit reached",
    "limit": "usage limit reached",
    "budget": "over its budget",
    "denied": "refused this model for the key",
}

ACCOUNT_NAMES = {"ollama": "Ollama Cloud", "anthropic": "Anthropic", "openai": "OpenAI"}


def _path():
    return home_data_dir() / "unusable.json"


def cooldown_seconds(settings) -> float:
    try:
        minutes = float(getattr(settings, "cloud_cooldown_minutes", DEFAULT_MINUTES))
    except (TypeError, ValueError):
        minutes = DEFAULT_MINUTES
    return max(1.0, minutes) * 60.0


def account_label(key: str) -> str:
    if key.startswith("profile:"):
        return key.split(":", 1)[1]
    return ACCOUNT_NAMES.get(key, key)


# ---------------------------------------------------------------- the shared store

def _read() -> dict:
    """{key: {until, kind, reason, at}, "_checked": {account: epoch}} — cached until the file changes."""
    path = _path()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {}
    if _cache["mtime"] == mtime:
        return _cache["data"]
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    _cache.update(mtime=mtime, data=data)
    return data


@contextlib.contextmanager
def _editing():
    with _lock:
        data = dict(_read())
        yield data
        now = time.time()
        data = {k: v for k, v in data.items()
                if k == "_checked" or (isinstance(v, dict) and float(v.get("until", 0)) > now)}
        path = _path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, indent=1))
            os.replace(tmp, path)
        except OSError:
            pass
        _cache.update(mtime=None)


def key_for(mc) -> str:
    """The account a profile is billed to, or "" for a free (local) model."""
    from . import spend
    try:
        return spend.account_for(mc)
    except Exception:  # noqa: BLE001
        return ""


def mark(mc, kind: str, reason: str, settings=None, seconds: float | None = None) -> dict | None:
    """Remember that `mc`'s account (or, for `denied`, the profile) can't be used for a while. Local models are
    never marked here: they are not 'used up', and their outages are handled per process (session.cool_down)."""
    account = key_for(mc)
    if not account:
        return None
    key = f"profile:{mc.name}" if kind == "denied" else account
    full = cooldown_seconds(settings)
    wait = full if not seconds else min(max(float(seconds), 60.0), full)
    entry = {"until": time.time() + wait, "kind": kind, "reason": " ".join(str(reason).split())[:300],
             "at": time.time(), "profile": mc.name}
    with _editing() as data:
        data[key] = entry
    return entry


def clear(key: str | None = None) -> None:
    """Forget one verdict, or all of them (and when accounts were last checked)."""
    with _editing() as data:
        if key is None:
            data.clear()
        else:
            data.pop(key, None)
            (data.get("_checked") or {}).pop(key, None)


def entries() -> dict:
    """Unexpired verdicts, {key: entry}."""
    now = time.time()
    return {k: v for k, v in _read().items()
            if k != "_checked" and isinstance(v, dict) and float(v.get("until", 0)) > now}


def unusable(mc) -> dict | None:
    """The verdict that keeps `mc` out of use right now, or None."""
    if mc is None:
        return None
    live = entries()
    if not live:
        return None
    hit = live.get(f"profile:{mc.name}")
    if hit:
        return hit
    account = key_for(mc)
    return live.get(account) if account else None


def unusable_name(name: str) -> dict | None:
    """unusable() for a profile name ([models.NAME] in the config)."""
    if not name or not entries():
        return None
    try:
        from .config import load_config
        return unusable(load_config().model(name))
    except (KeyError, SystemExit, Exception):  # noqa: BLE001 - an unknown name is simply not marked
        return None


def until_text(entry: dict) -> str:
    return time.strftime("%H:%M", time.localtime(float(entry.get("until", 0))))


def describe(entry: dict, key: str = "") -> str:
    who = account_label(key) if key else entry.get("profile", "")
    return f"{who}: {KIND_TEXT.get(entry.get('kind'), entry.get('kind', 'unusable'))} — skipped until {until_text(entry)}"


# ---------------------------------------------------------------- what an error means

_STATUS = re.compile(r"\b(40[1-3]|429)\b")


def classify(exc) -> tuple[str, float | None] | None:
    """(kind, seconds) when an error says the account itself can't be used — not a hiccup, not a bad request,
    not a short rate limit the normal wait handles. None otherwise."""
    from . import llm
    if isinstance(exc, llm.TransientError):
        return None
    text = str(exc)
    if isinstance(exc, llm.RateLimitError):
        if not exc.long_term:
            return None
        return ("credit" if re.search(r"credit|billing|insufficient_quota|add (usage )?credits", text, re.I)
                else "limit"), exc.retry_after
    if not isinstance(exc, llm.ProviderError):
        return None
    m = _STATUS.search(text[:80])
    status = int(m.group(1)) if m else 0
    low = text.lower()
    if status == 401 or "invalid api key" in low or "invalid x-api-key" in low:
        return "key", None
    if status == 402 or llm._LONG_TERM.search(text):
        return "credit", None
    if status == 403:
        return "denied", None
    return None


def note_failure(mc, exc, settings=None) -> dict | None:
    """Mark `mc` unusable when `exc` says its account can't answer; the entry, or None."""
    verdict = classify(exc)
    if verdict is None:
        return None
    kind, seconds = verdict
    return mark(mc, kind, str(exc), settings, seconds)


# ---------------------------------------------------------------- the check at session start

def _profiles_in_use(session) -> list:
    """Every profile this session may send a request to: its model and fallbacks, the router chain, overflow,
    planner/executor/summary/subagent/cloud-fallback models, describe_image and the tool models."""
    from . import planner
    st = session.settings
    names: list[str] = [session.model_config.name, *(getattr(session.model_config, "fallbacks", ()) or ())]
    for attr in ("router_profile", "overflow_profile", "planner_profile", "executor_profile", "compact_profile",
                 "subagent_profile", "cloud_fallback_profile", "fallback_profile", "worker_fallback_profile"):
        if getattr(st, attr, ""):
            names.append(getattr(st, attr))
    for rule in (getattr(st, "tool_models", {}) or {}).values():
        if isinstance(rule, str):
            names.append(rule)
        elif isinstance(rule, dict):
            names.extend(str(n) for n in (rule.get("models") or []))
    out, seen = [], set()
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        mc = session.model_config if name == session.model_config.name else planner._config_profile(session, name)
        if mc is not None:
            out.append(mc)
            for fb in (getattr(mc, "fallbacks", ()) or ()):
                if fb not in seen:
                    names.append(fb)            # walked by the loop: the list grows while it is read
    return out


def _probe_one(mc, settings) -> Exception | None:
    """One tiny request. The error it raised, or None when the account answered (or the answer was slow —
    slowness is not a verdict)."""
    from . import llm
    try:
        provider = llm.make_provider(mc, settings)
    except SystemExit as exc:
        return llm.ProviderError(f"{mc.name}: {exc}")
    try:
        stream = provider.stream([{"role": "user", "content": "Reply with: ok"}], "", [], model=mc.model,
                                 max_output_tokens=16, extra_body=mc.extra_body, timeout=PROBE_TIMEOUT)
        for _event in stream:
            break                              # the first event means the account accepted the request
        close = getattr(stream, "close", None)
        if close:
            close()
    except Exception as exc:  # noqa: BLE001 - the caller decides what it means
        return exc
    return None


def check_accounts(session, force: bool = False) -> list[tuple[str, dict]]:
    """Check each cloud account the session may use: the budget stop first (no request), then one tiny request
    to a profile on the account, all at once. Returns the (key, entry) pairs that are unusable now."""
    from . import spend
    by_account: dict[str, object] = {}
    for mc in _profiles_in_use(session):
        account = key_for(mc)
        if account and account not in by_account:
            by_account[account] = mc
    checked = dict(_read().get("_checked") or {})
    now = time.time()
    to_probe = []
    for account, mc in by_account.items():
        if unusable(mc):
            continue                                          # already known: no request needed
        try:
            reason = spend.over_budget(session.settings, account)
        except Exception:  # noqa: BLE001 - a broken meter must not block anything
            reason = ""
        if reason:
            mark(mc, "budget", reason, session.settings)
            continue
        if not force and now - float(checked.get(account, 0)) < PROBE_FRESH_SECONDS:
            continue
        to_probe.append((account, mc))
    results: dict[str, Exception | None] = {}
    threads = []
    for account, mc in to_probe:
        def run(a=account, m=mc):
            results[a] = _probe_one(m, session.settings)
        t = threading.Thread(target=run, daemon=True, name=f"hexiel-probe-{account}")
        t.start()
        threads.append(t)
    deadline = time.time() + PROBE_TIMEOUT + 5
    for t in threads:
        t.join(max(0.1, deadline - time.time()))
    with _editing() as data:
        stamps = data.setdefault("_checked", {})
        for account, _mc in to_probe:
            if account in results:
                stamps[account] = time.time()
    for account, mc in to_probe:
        exc = results.get(account)
        if exc is not None:
            note_failure(mc, exc, session.settings)
    live = entries()
    return [(k, v) for k, v in live.items() if k in by_account or
            (k.startswith("profile:") and any(m.name == k[8:] for m in by_account.values()))]


def probing_disabled() -> bool:
    return os.environ.get("HEXIEL_CLOUD_PROBE", "").strip().lower() in ("0", "off", "no", "false")


def start(session) -> None:
    """Begin the session-start check in the background; session.apply_cloud_check() joins it on the first turn."""
    if probing_disabled() or getattr(session, "internal", False):
        return
    box: dict = {}

    def run():
        try:
            box["found"] = check_accounts(session)
        except Exception as exc:  # noqa: BLE001 - the check is a convenience, never a crash
            box["error"] = exc

    t = threading.Thread(target=run, daemon=True, name="hexiel-cloud-check")
    t.start()
    session.__dict__["_cloud_check"] = (t, box)


def status_lines(session=None) -> list[str]:
    live = entries()
    if not live:
        return ["☁ every cloud account is usable (none is being skipped)"]
    minutes = int(cooldown_seconds(getattr(session, "settings", None)) // 60)
    out = [f"☁ skipped cloud accounts (cloud_cooldown_minutes = {minutes}):"]
    for key, entry in sorted(live.items(), key=lambda kv: kv[1].get("until", 0)):
        out.append(f"  • {describe(entry, key)}")
        if entry.get("reason"):
            out.append(f"      {entry['reason'][:160]}")
    out.append("  /accounts recheck forgets these and checks again now")
    return out


def command(session, arg: str = "") -> str:
    """/accounts — the skipped accounts; /accounts recheck forgets them and checks every account again now."""
    if arg.strip().lower() in ("recheck", "check", "retry", "clear"):
        clear()
        check_accounts(session, force=True)
        return "\n".join(["☁ checked every cloud account this session uses again:", *status_lines(session)])
    return "\n".join(status_lines(session))
