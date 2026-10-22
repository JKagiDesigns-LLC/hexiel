"""Send hexiel's own errors and the user's bug reports to the relay for tracking.

An error hexiel hits (a crashed turn, a tool that raised) is already saved locally by heal.py; this
sends a scrubbed copy to remote.hexiel.tech so it can be logged and tracked. Signed in to Hexiel
Remote, it is attached to the account (the user sees it and its status on hexiel.tech/support);
otherwise it goes to the public general queue — but only when `error_reports = "all"`, because
sending from an anonymous install needs the user's say-so. `/bug` and `/feature` always send
because the user asked. Nothing here may ever break or slow a session: every path is best-effort."""

import hashlib
import json
import os
import platform
import re
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .config import home_data_dir

MODES = ("account", "all", "off")
MAX_OUTBOX = 50
_HOME_PATHS = re.compile(r"(?i)(?:[a-z]:[\\/]+users[\\/]+|/users/|/home/)[^\\/\s'\":]+")
_SECRETS = [
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "<jwt>"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"), "<key>"),
    (re.compile(r"\b(?:ghp|gho|ghs|github_pat|glpat|xox[a-z]|hxd)[-_][A-Za-z0-9_-]{10,}"), "<token>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<key>"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}"), r"\1 <token>"),
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|passw(?:or)?d|authorization)(\"?\s*[:=]\s*\"?)[^\s\"',;]{6,}"),
     r"\1\2<redacted>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"\b(?:10|127|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?:\.\d{1,3})?\b"), "<ip>"),
    (re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}"), "<blob>"),
]
_notice_shown = False


# ---------------------------------------------------------------- scrubbing

def redact(text: str) -> str:
    """Remove what must not leave the machine: home paths, user names, keys, tokens, e-mails, LAN IPs."""
    text = str(text)
    for home in {str(Path.home()), str(Path.home()).replace("\\", "/")}:
        if len(home) > 3:
            text = text.replace(home, "~")
    text = _HOME_PATHS.sub("~", text)
    for pattern, repl in _SECRETS:
        text = pattern.sub(repl, text)
    return text


def scrub_context(context: dict | None) -> str:
    """Keys and sizes only: argument values and the user's prompts can hold their code and secrets."""
    if not context:
        return ""
    return json.dumps({k: (f"<{type(v).__name__}, {len(str(v))} chars>" if not isinstance(v, (int, float, bool)) else v)
                       for k, v in context.items()}, sort_keys=True)


def fingerprint(kind: str, detail: str, traceback_text: str = "") -> str:
    """Same bug -> same fingerprint, whatever the paths, ids, numbers or line numbers."""
    frames = re.findall(r'File "[^"]*[\\/]([^\\/"]+)", line \d+, in (\w+)', traceback_text)[-3:]
    shape = re.sub(r"\d+|0x[0-9a-f]+|'[^']*'|\"[^\"]*\"", "#", detail.split("\n")[0])[:120]
    return hashlib.sha256(f"{kind}|{shape}|{frames}".encode()).hexdigest()[:32]


def environment() -> str:
    return (f"hexiel {__version__} · python {platform.python_version()} · "
            f"{platform.system()} {platform.release()} {platform.machine()}")


# ---------------------------------------------------------------- configuration

def mode() -> str:
    """account (default) | all | off — HEXIEL_NO_REPORTS=1 always wins."""
    if os.environ.get("HEXIEL_NO_REPORTS") == "1":
        return "off"
    try:
        from .config import load_config
        value = str(getattr(load_config().settings, "error_reports", "account")).lower()
    except (Exception, SystemExit):                      # a broken config must not break reporting's caller
        return "account"
    return value if value in MODES else "account"


def _remote() -> dict:
    from . import remote
    return remote.load_config()


def relay_url() -> str:
    from .remote import DEFAULT_RELAY
    return os.environ.get("HEXIEL_RELAY") or _remote().get("relay") or DEFAULT_RELAY


def signed_in() -> bool:
    return bool(_remote().get("device_token"))


def _headers() -> dict:
    token = _remote().get("device_token")
    return {"Authorization": f"Bearer {token}"} if token else {}


# ---------------------------------------------------------------- sending

def outbox() -> Path:
    return home_data_dir() / "reports-outbox"


def _post(report: dict, timeout: float = 8.0) -> dict | None:
    import httpx
    try:
        resp = httpx.post(relay_url().rstrip("/") + "/api/reports", json=report, headers=_headers(), timeout=timeout)
    except httpx.HTTPError:
        return None
    if resp.status_code == 401 and _headers():            # revoked device token: still report, anonymously
        try:
            resp = httpx.post(relay_url().rstrip("/") + "/api/reports", json=report, timeout=timeout)
        except httpx.HTTPError:
            return None
    try:
        return resp.json() if resp.status_code == 200 else {"error": resp.json().get("error", resp.text)}
    except ValueError:
        return None


def _queue(report: dict) -> None:
    try:
        box = outbox()
        box.mkdir(parents=True, exist_ok=True)
        for old in sorted(box.glob("*.json"))[:-MAX_OUTBOX + 1]:
            old.unlink(missing_ok=True)
        (box / f"{int(time.time() * 1000)}.json").write_text(json.dumps(report))
    except OSError:
        pass


def send(report: dict) -> dict | None:
    """Send now; keep it in the outbox for later when the relay can't be reached."""
    out = _post(report)
    if out is None:
        _queue(report)
    elif "error" not in out:
        flush_outbox()
    return out


def flush_outbox() -> int:
    sent = 0
    try:
        files = sorted(outbox().glob("*.json"))
    except OSError:
        return 0
    for path in files:
        try:
            out = _post(json.loads(path.read_text()))
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            continue
        if out is None:
            break                                          # still offline: try again next time
        path.unlink(missing_ok=True)
        sent += 1
    return sent


def in_background(fn, *args) -> None:
    threading.Thread(target=lambda: _quiet(fn, *args), daemon=True, name="hexiel-reporter").start()


def _quiet(fn, *args):
    try:
        fn(*args)
    except Exception:                                      # noqa: BLE001 - reporting never raises into a session
        pass


# ---------------------------------------------------------------- what hexiel reports

def auto_report(incident: dict) -> bool:
    """Called for every recorded incident. Returns whether a report was started."""
    global _notice_shown
    setting = mode()
    if setting == "off":
        return False
    if setting == "account" and not signed_in():
        if not _notice_shown and sys.stderr is not None and sys.stderr.isatty():
            _notice_shown = True
            sys.stderr.write("hexiel hit an error (saved locally). Send it for tracking with /bug, or set "
                             "error_reports = \"all\" in your config to send these automatically.\n")
        return False
    kind = str(incident.get("kind", "error"))
    detail = redact(incident.get("detail", ""))
    trail = redact(incident.get("traceback", ""))
    report = {"kind": "error", "source": "auto", "title": f"{kind}: {detail.splitlines()[0] if detail else 'error'}"[:200],
              "fingerprint": fingerprint(kind, incident.get("detail", ""), incident.get("traceback", "")),
              "details": "\n".join(filter(None, [environment(), f"incident: {kind}", detail, trail,
                                                  scrub_context(incident.get("context"))])),
              "version": __version__, "platform": f"{platform.system()} {platform.machine()}"}
    in_background(send, report)
    return True


def file_report(kind: str, text: str, recent_incidents: list[dict] | None = None) -> str:
    """/bug and /feature. Returns the line to show the user."""
    text = redact(text.strip())
    if not text:
        return f"usage: /{'bug' if kind == 'bug' else 'feature'} <what happened / what you would like>"
    details = [environment()]
    for incident in (recent_incidents or [])[-3:]:
        details.append(f"recent incident {incident.get('kind')}: " + redact(incident.get("detail", ""))[:300])
    report = {"kind": kind, "source": "manual", "title": text.splitlines()[0][:120], "body": text,
              "details": "\n".join(details), "version": __version__,
              "platform": f"{platform.system()} {platform.machine()}"}
    out = send(report)
    if out is None:
        return "couldn't reach the relay — saved, and it will be sent next time hexiel can connect"
    if "error" in out:
        return f"report not accepted: {out['error']}"
    where = ("https://hexiel.tech/support (sign in with your remote.hexiel.tech login to see your reports)"
             if signed_in() else "https://hexiel.tech/support")
    return f"thanks — logged as {out['id']}. Track it at {where}"


def my_reports_text() -> str:
    """/bugs — the signed-in account's reports and whether they are resolved."""
    if not signed_in():
        return "sign in with `hexiel remote login` to see your reports here, or browse https://hexiel.tech/support"
    import httpx
    try:
        resp = httpx.get(relay_url().rstrip("/") + "/api/reports/mine", headers=_headers(), timeout=10)
    except httpx.HTTPError:
        return "couldn't reach the relay right now"
    if resp.status_code == 402:
        return "your Hexiel Remote subscription isn't active, so your reports can't be listed"
    if resp.status_code != 200:
        return f"couldn't list your reports ({resp.status_code})"
    items = resp.json().get("items", [])
    if not items:
        return "no reports yet"
    lines = []
    for r in items[:20]:
        note = f" — fixed in {r['fixed_in']}" if r.get("fixed_in") else ""
        extra = f" (seen {r['count']}×)" if r.get("count", 1) > 1 else ""
        lines.append(f"[{r['status']}] {r['kind']}: {r['title']}{extra}{note}")
        if r.get("resolution"):
            lines.append(f"    {r['resolution']}")
    return "\n".join(lines)
