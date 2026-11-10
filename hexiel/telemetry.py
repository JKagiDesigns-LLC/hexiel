"""Anonymous install/upgrade telemetry.

The very first time hexiel runs on a machine we send a public ping to
remote.hexiel.tech so the website can show real install and upgrade counts.
Nothing identifiable is sent: machine_id is a hash of a stable machine
fingerprint, not a user id or serial number. The ping is best-effort and
never blocks startup."""

import hashlib
import json
import os
import platform
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx

from . import __version__
from .config import home_data_dir

PING_URL = "https://remote.hexiel.tech/api/public/ping"
PING_TIMEOUT = 5.0
RETRY_AFTER = 3600.0          # after a failed ping, wait this long before trying again
STORAGE: Path | None = None   # tests override; None = <data dir>/telemetry.json, resolved per call


def _storage() -> Path:
    return STORAGE if STORAGE is not None else home_data_dir() / "telemetry.json"


def _disabled() -> bool:
    """Opt-out: DO_NOT_TRACK=1 (the community standard) or HEXIEL_TELEMETRY=off."""
    if os.environ.get("DO_NOT_TRACK", "").strip().lower() in ("1", "true", "yes"):
        return True
    return os.environ.get("HEXIEL_TELEMETRY", "").strip().lower() in ("0", "off", "false", "no")


def _machine_fingerprint() -> str:
    """Stable, anonymous machine identifier. Uses boot session + machine id if available,
    falling back to a generated id stored in data dir."""
    parts = []
    # Linux: /etc/machine-id or /var/lib/dbus/machine-id (not sensitive, widely used)
    for p in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        try:
            parts.append(p.read_text().strip())
            break
        except OSError:
            pass
    # macOS: IOPlatformUUID is readable without privileges via ioreg; skip on other platforms
    if sys.platform == "darwin" and not parts:
        import subprocess  # noqa: S404
        try:
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=3
            ).stdout
            for line in out.splitlines():
                if "IOPlatformUUID" in line and '"' in line:
                    parts.append(line.split('"')[-2])
                    break
        except Exception:  # noqa: BLE001
            pass
    # Windows: MachineGuid from registry
    if sys.platform == "win32" and not parts:
        try:
            import winreg  # type: ignore[import-not-found]
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Cryptography") as key:
                parts.append(winreg.QueryValueEx(key, "MachineGuid")[0])
        except Exception:  # noqa: BLE001
            pass
    # Fallback: keep a generated id in the data dir so the same install doesn't double-count.
    fallback = home_data_dir() / ".machine-id"
    if not parts:
        try:
            if fallback.exists():
                parts.append(fallback.read_text().strip())
            else:
                gen = uuid.uuid4().hex
                fallback.parent.mkdir(parents=True, exist_ok=True)
                fallback.write_text(gen)
                parts.append(gen)
        except OSError:
            parts.append("unknown")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _event_kind() -> str:
    """Decide whether this run is the first install on this machine or an upgrade.
    We store the last version we pinged for; if none -> install, if different -> upgrade,
    if same -> already counted."""
    try:
        state = json.loads(_storage().read_text())
    except (OSError, ValueError):
        state = {}
    last = state.get("version")
    if last is None:
        return "install"
    return "upgrade" if last != __version__ else ""


def _record_ping(event: str) -> None:
    try:
        state = json.loads(_storage().read_text())
    except (OSError, ValueError):
        state = {}
    state["version"] = __version__
    state["last_event"] = event
    state["last_ping"] = time.time()
    try:
        st = _storage()
        st.parent.mkdir(parents=True, exist_ok=True)
        tmp = st.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, st)
    except OSError:
        pass


def _note_attempt() -> None:
    """Remember when we last tried, so an offline machine is not retried on every launch."""
    try:
        state = json.loads(_storage().read_text())
    except (OSError, ValueError):
        state = {}
    state["last_attempt"] = time.time()
    try:
        st = _storage()
        st.parent.mkdir(parents=True, exist_ok=True)
        st.write_text(json.dumps(state, indent=2))
    except OSError:
        pass


def _recently_tried() -> bool:
    try:
        last = float(json.loads(_storage().read_text()).get("last_attempt", 0))
    except (OSError, ValueError, TypeError):
        return False
    return 0 <= time.time() - last < RETRY_AFTER


def ping() -> None:
    """Send one anonymous install/upgrade ping if needed. Never raises.

    The ping counts as delivered only when the server answers 2xx (or a permanent 4xx, which a
    retry cannot fix); a network error or 5xx/404 leaves it pending and it is retried later."""
    try:
        if _disabled():
            return
        event = _event_kind()
        if not event or _recently_tried():
            return
        payload = {
            "machine_id": _machine_fingerprint(),
            "event": event,
            "version": __version__,
            "platform": platform.system(),
        }
        _note_attempt()
        resp = httpx.post(PING_URL, json=payload, timeout=PING_TIMEOUT)
        if resp.status_code < 300 or resp.status_code in (400, 422):
            _record_ping(event)
    except Exception:  # noqa: BLE001 - telemetry is best-effort
        pass


def ping_in_background() -> None:
    """Run ping() on a daemon thread so a slow or unreachable server never delays startup."""
    threading.Thread(target=ping, name="hexiel-telemetry", daemon=True).start()
