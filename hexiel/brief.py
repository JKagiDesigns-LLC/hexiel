"""The project brief attached to the first message of a session, built locally in a few hundred milliseconds:
what kind of project this is, its entry points, how to run its tests, the latest commits, open todo items and
the machine. It saves the model the usual opening round of ls / cat package.json / git log calls."""

from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404 - fixed argv only
import time
from pathlib import Path

BUDGET = 0.6           # seconds; any part not done by then is left out
MAX_CHARS = 1600

MARKERS = [("pyproject.toml", "Python"), ("setup.py", "Python"), ("requirements.txt", "Python"),
           ("package.json", "JavaScript/TypeScript"), ("go.mod", "Go"), ("Cargo.toml", "Rust"),
           ("pom.xml", "Java (Maven)"), ("build.gradle", "Java/Kotlin (Gradle)"), ("build.gradle.kts", "Kotlin (Gradle)"),
           ("Gemfile", "Ruby"), ("composer.json", "PHP"), ("CMakeLists.txt", "C/C++ (CMake)"), ("mix.exs", "Elixir"),
           ("pubspec.yaml", "Dart/Flutter"), ("Package.swift", "Swift"), ("deno.json", "Deno")]


def _kind(root: Path) -> list[str]:
    kinds = [k for f, k in MARKERS if (root / f).exists()]
    if any(root.glob("*.sln")) or any(root.glob("*.csproj")):
        kinds.append(".NET")
    if (root / "Dockerfile").exists() or (root / "compose.yaml").exists() or (root / "docker-compose.yml").exists():
        kinds.append("containerized")
    return list(dict.fromkeys(kinds))


def _entry_points(root: Path) -> list[str]:
    out = []
    py = root / "pyproject.toml"
    if py.exists():
        try:
            import tomllib
            data = tomllib.loads(py.read_text(errors="replace"))
            proj = data.get("project", {})
            for name, target in (proj.get("scripts") or {}).items():
                out.append(f"`{name}` → {target}")
            if proj.get("name"):
                out.insert(0, f"package {proj['name']} {proj.get('version', '')}".strip())
        except (ValueError, OSError):
            pass
    pkg = root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(errors="replace"))
            if data.get("main"):
                out.append(f"main: {data['main']}")
            scripts = data.get("scripts") or {}
            if scripts:
                out.append("npm scripts: " + ", ".join(list(scripts)[:8]))
        except (ValueError, OSError):
            pass
    for cand in ("main.py", "app.py", "manage.py", "main.go", "cmd", "src/main.rs", "src/index.ts", "src/main.ts",
                 "index.js", "Makefile"):
        if (root / cand).exists():
            out.append(cand)
    return out[:8]


def _git(root: Path, *args: str, timeout: float = 1.5) -> str:
    if not (root / ".git").exists():
        return ""
    try:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,  # nosec B603 B607
                             timeout=timeout, check=False)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _todos(root: Path) -> list[str]:
    for name in ("TODO.md", "todo.md", "TODO", "docs/todo.md"):
        p = root / name
        if p.is_file() and p.stat().st_size < 400_000:
            items = re.findall(r"^\s*[-*] \[ \] (.+)$", p.read_text(errors="replace"), re.M)
            if items:
                return [f"{name}: {len(items)} open — " + "; ".join(i.strip()[:80] for i in items[:3])]
    return []


def build(cwd: str, chain=None) -> str:
    """The brief, or "" for a folder that isn't a project (home, /, an empty dir)."""
    root = Path(cwd)
    began = time.time()
    try:
        if root.resolve() in (Path.home().resolve(), Path(root.resolve().anchor)):
            return ""
    except OSError:
        return ""
    kinds = _kind(root)
    if not kinds and not (root / ".git").exists():
        return ""
    lines = [f"[project brief, gathered locally — {root.resolve().name}]"]
    if kinds:
        lines.append("type: " + ", ".join(kinds))
    entries = _entry_points(root)
    if entries:
        lines.append("entry points: " + "; ".join(entries))
    runners = list(getattr(chain, "test_runners", None) or [])
    if not runners and time.time() - began < BUDGET:
        try:
            from .tooling import _detect_test_runners
            runners = _detect_test_runners(cwd)
        except Exception:  # noqa: BLE001
            runners = []
    if runners:
        lines.append("tests: " + "; ".join(" ".join(r.command) for r in runners[:3]) + " (use the test tool)")
    if time.time() - began < BUDGET:
        branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
        log = _git(root, "log", "-5", "--format=%h %ar %s")
        dirty = _git(root, "status", "--porcelain")
        if log:
            n = len(dirty.splitlines()) if dirty else 0
            lines.append(f"git: on {branch or '?'}, {n} uncommitted change(s); recent commits:")
            lines.extend("  " + ln[:110] for ln in log.splitlines())
    if time.time() - began < BUDGET:
        lines.extend(_todos(root))
    if time.time() - began < BUDGET:
        try:
            from . import inventory
            lines.append("machine: " + inventory.system_line())
        except Exception:  # noqa: BLE001
            pass
    text = "\n".join(lines)
    return text[:MAX_CHARS]
