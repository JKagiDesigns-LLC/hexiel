"""Opt-in sandbox for shell commands (0.8.7 #40).

`sandbox = "off"` (default) changes nothing. Two opt-in profiles wrap every
shell command (bash, python, powershell, test suites) so an autonomous run
works inside a box; approval gates still apply on top, exactly as before:

  sandbox = "safe"       macOS: sandbox-exec (Seatbelt) — read everywhere,
                          write only the project + /tmp.
                        Linux: bubblewrap when installed — root filesystem
                          read-only, the project and /tmp writable (runs
                          unprivileged; when user namespaces are disabled,
                          doctor points at the kernel setting).
  sandbox = "container"  docker or podman: a one-shot container, project
                          mounted at /work. Needs `sandbox_image` in config.

tools.py applies the wrap at its single choke point (`_run_shell`), so
background tasks, parked timeouts and the test-runner path all inherit the
box. (Linux landlock stays a backlog idea — bubblewrap covers the need.)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MODE = "off"                      # "off" | "safe" | "container" — set at boot from [settings]
IMAGE = ""                        # sandbox = "container": the image to run commands in
CONTAINER_WORK = "/work"          # the project's mount point inside a container

BWRAP_HINT = ("enable user namespaces (sysctl kernel.unprivileged_userns_clone=1) or install "
              "bubblewrap (apt install bubblewrap); or use sandbox = \"container\"")


def available() -> dict[str, bool]:
    """What sandbox infra exists on this machine (doctor + tool notes)."""
    return {
        "sandbox-exec": bool(shutil.which("sandbox-exec")),        # macOS seatbelt
        "bubblewrap": bool(shutil.which("bwrap")),                 # linux boxes-in-a-box
        "docker": bool(shutil.which("docker")),
        "podman": bool(shutil.which("podman")),
    }


def _bwrap_argv(cwd: str, argv: list[str]) -> list[str]:
    """Unprivileged on Linux: everything readable, the project + /tmp writable."""
    return [
        "bwrap",
        "--ro-bind", "/", "/",
        "--dev-bind", "/dev", "/dev",       # devices: builds want /dev/null, /dev/tty
        "--proc", "/proc",
        "--bind", str(os.path.abspath(cwd)), str(os.path.abspath(cwd)),
        "--bind", tempfile.gettempdir(), tempfile.gettempdir(),
        "--",
        *argv,
    ]


def _seatbelt_argv(cwd: str, argv: list[str]) -> list[str]:
    """macOS: read everywhere, write only the project + /tmp (plus the usual system bits)."""
    profile = tempfile.NamedTemporaryFile(prefix="hexiel-sandbox-", suffix=".sb",
                                          delete=False, mode="w")
    profile.write("\n".join(Seatbelt.profile(cwd)))
    profile.close()
    return ["sandbox-exec", "-f", profile.name, *argv]


class Seatbelt:
    """The (deny default)+allow profile as statements; shared with tests."""

    @staticmethod
    def profile(cwd: str, tmp: str | None = None) -> list[str]:
        tmp = tmp or tempfile.gettempdir()
        return [
            "(version 1)",
            "(deny default)",
            "(allow file-read*)",
            "(allow network*)",
            "(allow process*)",
            "(allow system*)",
            f'(allow file-write* (subpath "{cwd}") (subpath "{tmp}") (subpath "/private/tmp"))',
        ]


def _runtime() -> str:
    return next((r for r in ("docker", "podman") if shutil.which(r)), "")


def _container_argv(cwd: str, argv: list[str]) -> list[str]:
    work = CONTAINER_WORK
    return [shutil.which(_runtime()), "run", "--rm",
            "-v", f"{os.path.abspath(cwd)}:{work}", "-w", work,
            *(["-e", "HOME=/tmp"] if argv[:1] == ["/bin/bash"] else []),
            IMAGE or "python:3.12-slim", *argv]


def wrap(cwd: str, argv: list[str]) -> tuple[list[str], str]:
    """The argv to actually run plus a warning note ('' when all went to plan).
    Falls back to running unsandboxed — with an explicit note — when the infra is
    missing or misconfigured: a blocked command is worse than a loud run for a
    coding agent (it would invent workarounds)."""
    if MODE == "off" or not argv:
        return argv, ""
    if MODE == "safe":
        if sys.platform == "darwin":
            if not shutil.which("sandbox-exec"):
                return argv, "sandbox=safe asked but sandbox-exec is missing (macOS only) — running unsandboxed"
            return _seatbelt_argv(cwd, argv), ""
        if sys.platform.startswith("linux"):
            if not shutil.which("bwrap"):
                return argv, f"sandbox=safe asked but bubblewrap is missing — {BWRAP_HINT} (running unsandboxed)"
            return _bwrap_argv(cwd, argv), ""
        return argv, f"sandbox=safe has no profile for {sys.platform} — running unsandboxed"
    if MODE == "container":
        if not shutil.which(_runtime()):
            return argv, "sandbox=container asked but neither docker nor podman is installed — running unsandboxed"
        return _container_argv(cwd, argv), ""
    return argv, ""


def apply(settings) -> None:
    """Boot wiring: [settings] decides this session's mode."""
    global MODE, IMAGE
    mode = str(getattr(settings, "sandbox", "off") or "off").lower()
    MODE = {"safe": "safe", "container": "container"}.get(mode, "off")
    IMAGE = str(getattr(settings, "sandbox_image", "") or "")
