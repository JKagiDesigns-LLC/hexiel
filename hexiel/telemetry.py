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
import time
import uuid
from pathlib import Path

import httpx

from . import __version__
from .config import home_data_dir

PING_URL = "https://remote.hexiel.tech/api/public/ping"
PING_TIMEOUT = 5.0
STORAGE = home_data_dir() / "telemetry.json"


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
        state = json.loads(STORAGE.read_text())
    except (OSError, ValueError):
        state = {}
    last = state.get("version")
    if last is None:
        return "install"
    return "upgrade" if last != __version__ else ""


def _record_ping(event: str) -> None:
    try:
        state = json.loads(STORAGE.read_text())
    except (OSError, ValueError):
        state = {}
    state["version"] = __version__
    state["last_event"] = event
    state["last_ping"] = time.time()
    try:
        STORAGE.parent.mkdir(parents=True, exist_ok=True)
        STORAGE.write_text(json.dumps(state, indent=2))
    except OSError:
        pass


def ping() -> None:
    """Send one anonymous install/upgrade ping if needed. Never raises."""
    event = _event_kind()
    if not event:
        return
    payload = {
        "machine_id": _machine_fingerprint(),
        "event": event,
        "version": __version__,
        "platform": platform.system(),
    }
    try:
        httpx.post(PING_URL, json=payload, timeout=PING_TIMEOUT)
        _record_ping(event)
    except Exception:  # noqa: BLE001 - telemetry is best-effort
        pass
