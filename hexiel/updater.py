"""Self-update: find the newest released version on GitLab, install it, restart.

A git checkout (`git clone` + `pip install -e`) updates with `git pull --ff-only`
and refuses when there are local changes; a `pip install hexiel` install
upgrades from PyPI. Checks are cached so the
network is asked at most every few hours, and never block startup."""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

from . import __version__
from .config import home_data_dir

PROJECT = "jkagidesigns1/public/applications/hexiel"
TAGS_API = f"https://gitlab.com/api/v4/projects/{PROJECT.replace('/', '%2F')}/repository/tags"
CHECK_EVERY = 6 * 3600
REPO = Path(__file__).resolve().parent.parent


def parse_version(text: str) -> tuple[int, ...] | None:
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    return tuple(int(x) for x in m.groups()) if m else None


def latest_release(timeout: float = 5.0) -> str | None:
    """Newest vX.Y.Z tag on GitLab (public API, no token needed)."""
    resp = httpx.get(TAGS_API, params={"per_page": 100}, timeout=timeout)
    resp.raise_for_status()
    versions = [v for v in (parse_version(t["name"]) for t in resp.json()) if v]
    return ".".join(map(str, max(versions))) if versions else None


def check(force: bool = False) -> dict:
    """{"current", "latest", "available"}; cached for CHECK_EVERY seconds.
    Never raises — offline or rate-limited simply means "no update known"."""
    cache = home_data_dir() / "update-check.json"
    now = time.time()
    if not force:
        try:
            data = json.loads(cache.read_text())
            if now - data.get("checked", 0) < CHECK_EVERY:
                return _verdict(data.get("latest"))
        except (OSError, ValueError):
            pass
    try:
        latest = latest_release()
    except Exception:  # noqa: BLE001 - network/API trouble is not an error for the user
        return _verdict(None)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"checked": now, "latest": latest}))
    except OSError:
        pass
    return _verdict(latest)


def _verdict(latest: str | None) -> dict:
    cur, new = parse_version(__version__), parse_version(latest or "")
    return {"current": __version__, "latest": latest,
            "available": bool(cur and new and new > cur)}


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=120)


def is_git_checkout() -> bool:
    return (REPO / ".git").exists()


def apply_update() -> tuple[bool, str]:
    """Install the newest release. Returns (ok, log). Does not restart."""
    log: list[str] = []
    if is_git_checkout():
        dirty = _git("status", "--porcelain", "--untracked-files=no").stdout.strip()
        if dirty:
            return False, ("not updating: your hexiel checkout has local changes\n"
                           f"{dirty}\ncommit or stash them in {REPO}, then try again")
        pull = _git("pull", "--ff-only")
        log.append(pull.stdout.strip() or pull.stderr.strip())
        if pull.returncode != 0:
            return False, "git pull failed:\n" + "\n".join(log)
        _git("fetch", "--tags")
        cmd = [sys.executable, "-m", "pip", "install", "-q", "-e", f"{REPO}[serve,view]"]
    else:   # pip install: upgrade from PyPI
        cmd = [sys.executable, "-m", "pip", "install", "-q", "--upgrade", "hexiel[serve,view]"]
    pip = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    log.append(pip.stdout.strip() or pip.stderr.strip()[-800:] or "dependencies up to date")
    if pip.returncode != 0:
        return False, "install failed:\n" + "\n".join(log)
    ensure_platform_tools(log)   # clipboard helpers for image pasting (best effort)
    installed = _installed_version()
    log.append(f"installed hexiel {installed} — restart to use it")
    return True, "\n".join(x for x in log if x)


def _sudo_available() -> bool:
    import shutil
    if not shutil.which("sudo"):
        return False
    return subprocess.run(["sudo", "-n", "true"],
                          capture_output=True, timeout=10).returncode == 0


def ensure_platform_tools(log: list[str]) -> None:
    """Make sure the clipboard helpers image pasting relies on exist (wl-paste or xclip on
    Linux, pngpaste on macOS). Installs them non-interactively where that is possible and
    otherwise records the exact command for the user. Best effort — never raises."""
    import shutil
    if sys.platform == "win32":
        log.append("note: pasting images is a Linux/macOS feature (wl-paste/xclip/pngpaste); from Windows use WSL")
        return
    if shutil.which("wl-paste") or shutil.which("xclip") or shutil.which("pngpaste"):
        return
    pkgs: dict[str, list[str]] = {
        "apt-get": ["wl-clipboard"],
        "dnf": ["wl-clipboard"],
        "pacman": ["wl-clipboard"],
        "zypper": ["wl-clipboard"],
    }
    if sys.platform == "darwin":
        cmd = [["brew", "install", "pngpaste"]] if shutil.which("brew") else []
    else:
        cmd = []
        for pm, args in pkgs.items():
            if shutil.which(pm):
                cmd = [pm, "install", "-y", *args]
                if pm == "pacman":
                    cmd = ["pacman", "-S", "--noconfirm", *args]
                elif pm == "zypper":
                    cmd = ["zypper", "-n", "install", *args]
                break
    if not cmd:
        tools = ("pngpaste — brew install pngpaste" if sys.platform == "darwin"
                 else "wl-clipboard — e.g. apt/dnf install wl-clipboard, or xclip")
        log.append(f"note: pasting images needs a clipboard toolset ({tools}); install it and run /update again")
        return
    prefix: list[str] = []
    if os.geteuid() != 0 and sys.platform != "darwin":
        if not _sudo_available():
            log.append(f"note: pasting images needs {' '.join(cmd)} — run it manually (sudo) and /update will detect it")
            return
        prefix = ["sudo", "-n"]
    result = subprocess.run([*prefix, *cmd], capture_output=True, text=True, timeout=180)
    if result.returncode == 0:
        log.append(f"installed the clipboard helpers for pasting images ({' '.join([*prefix, *cmd])})")
    else:
        log.append(f"could not install clipboard helpers automatically ({' '.join(cmd)}): {result.stderr.strip()[-200:] or result.returncode}")


def _installed_version() -> str:
    """Version on disk now (the running process still holds the old one)."""
    try:
        text = (REPO / "hexiel" / "__init__.py").read_text()
        m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
        return m.group(1) if m else "?"
    except OSError:
        return "?"


def restart(extra_args: list[str] | None = None) -> None:
    """Replace this process with a fresh hexiel on the updated code, resuming
    the latest session of this project (-c). Never returns."""
    args = [a for a in sys.argv[1:] if a not in ("-c", "--continue")]
    os.execv(sys.executable, [sys.executable, "-m", "hexiel", *args, "-c", *(extra_args or [])])
