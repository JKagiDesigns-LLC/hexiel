"""Tell the person when another hexiel window is using the same local model.

A local model serves one request at a time, so with several hexiel windows (or background runs) open, requests queue up
silently and it looks like hexiel hung. Every request to a local model registers a small marker file in the data
folder; a request that finds other live markers for the same model says so. Cloud models are not tracked — they
serve everyone at once. Markers of dead processes, or older than the longest sensible request, are ignored."""

import hashlib
import ipaddress
import json
import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from .config import home_data_dir

STALE_SECONDS = 45 * 60


def is_local(mc) -> bool:
    """A model served from this machine or this network (where it is likely shared), not a cloud API."""
    if getattr(mc, "provider", "") == "anthropic" or not getattr(mc, "base_url", ""):
        return False
    if str(getattr(mc, "model", "")).endswith((":cloud", "-cloud")):      # Ollama Cloud models are routed through a local server
        return False                                                      # but run (and queue) in the cloud
    host = (urlparse(mc.base_url).hostname or "").lower()
    if host in ("localhost", "host.docker.internal") or host.endswith((".local", ".lan", ".internal", ".home", ".ts.net")) or "." not in host:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10")      # Tailscale


def folder() -> Path:
    return home_data_dir() / "busy"


def model_key(mc) -> str:
    return hashlib.sha1(f"{mc.base_url}|{mc.model}".encode(), usedforsecurity=False).hexdigest()[:16]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def others(mc, token: str = "") -> int:
    """How many other live requests are using this model right now."""
    count = 0
    try:
        files = list(folder().glob(f"{model_key(mc)}.*.json"))
    except OSError:
        return 0
    for path in files:
        try:
            info = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if info.get("token") == token:
            continue
        if time.time() - info.get("started", 0) > STALE_SECONDS or not _alive(int(info.get("pid", 0))):
            path.unlink(missing_ok=True)
            continue
        count += 1
    return count


class Slot:
    def __init__(self, path: Path | None, waiting: int) -> None:
        self.path, self.waiting = path, waiting

    def release(self) -> None:
        if self.path is not None:
            self.path.unlink(missing_ok=True)
            self.path = None


def enter(mc, say=None, label: str = "") -> Slot:
    """Register a request on a local model. `say(text)` is called (once) when someone else is already using it."""
    if not is_local(mc):
        return Slot(None, 0)
    token = uuid.uuid4().hex[:10]
    waiting = others(mc, token)
    path = None
    try:
        folder().mkdir(parents=True, exist_ok=True)
        path = folder() / f"{model_key(mc)}.{token}.json"
        path.write_text(json.dumps({"pid": os.getpid(), "token": token, "started": time.time(), "label": label}))
    except OSError:
        path = None
    if waiting and say:
        say(f"⏳ {mc.name} is busy with {waiting} other request{'s' if waiting != 1 else ''} from another hexiel "
            "window or background run — this one may wait its turn")
    return Slot(path, waiting)
