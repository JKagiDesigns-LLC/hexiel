"""Hexiel-made tools: the agent extends itself.

A made tool is a Python file in `<project>/.hexiel/tools/*.py` (or
`~/.config/hexiel/tools/*.py` for everywhere-tools) that defines:

    def run(args: dict, ctx) -> ToolResult | str

plus module-level

    TOOLS = [{"name": ..., "description": ..., "parameters": {...}, "readonly"?: false}]

The agent writes these itself with the `make_tool` tool; the loader imports
them exactly like native tools, hot-reloads changed files at the start of each
turn, and keeps the running session's tool list in sync (new names added,
changed ones replaced, deleted files removed). Made tools count as the same
trust tier as `bash` — they run real code in the project; `readonly` marks the
ones that only look things up, `always_ok` is always forced False.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

from .config import home_config_dir
from .tools import ToolDef

NAME_OK = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
MOUNT = ".hexiel/tools"                 # per-project location hexiel's make_tool writes to

# cwd -> fingerprint of the made-tool files (cheap turn-start change detector)
_fingerprints: dict[str, tuple] = {}


def dirs(cwd: str) -> list[Path]:
    """Where made tools live: the project first (project-specific), then the
    user's config (tools usable everywhere)."""
    return [Path(cwd) / MOUNT, home_config_dir() / MOUNT]


def fingerprint(cwd: str) -> tuple:
    """[(path, mtime_ns)] of every loader-visible tool file — a cheap change
    probe where an unchanged directory costs two stat calls."""
    steps = []
    for root in dirs(cwd):
        if root.is_dir():
            for path in sorted(root.glob("*.py")):
                try:
                    steps.append((str(path), path.stat().st_mtime_ns))
                except OSError:
                    pass
    return tuple(steps)


def load_file(path: Path) -> list[ToolDef]:
    """Import one tool file and return its ToolDef entries. A broken file
    contributes nothing — it must not break session boot either."""
    module_name = f"hexiel_tool_{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except Exception:                        # noqa: BLE001 - a broken made-tool must not kill boot
        return []
    out: list[ToolDef] = []
    for spec_dict in (getattr(module, "TOOLS", None) or []):
        if not isinstance(spec_dict, dict):
            continue
        tool = from_spec(spec_dict)
        if tool is not None:
            out.append(tool)
    return out


def from_spec(spec_dict: dict) -> ToolDef | None:
    """A spec dict from a made file -> a ToolDef, or None when it can't be one."""
    name = str(spec_dict.get("name", "")).strip()
    run = spec_dict.get("run")
    if not NAME_OK.match(name) or not callable(run):
        return None
    params = spec_dict.get("parameters") or {}
    if not isinstance(params, dict) or params.get("type") != "object":
        # lenient shorthand: {arg_name: {"type": ...}} (or bare type names) directly
        params = {"type": "object",
                  "properties": {k: (v if isinstance(v, dict) else {"type": "string"})
                                 for k, v in params.items()}}
    tool = ToolDef(name, str(spec_dict.get("description", "")).strip() or "(no description)",
                   params, run=run)
    tool.readonly = bool(spec_dict.get("readonly", False))    # used for the approval gate
    tool.always_ok = False                                     # never housekeeping: it asks like any tool
    tool.made = True                                           # tag: refreshed/replaced by custom_tools
    return tool


def discover(cwd: str) -> list[ToolDef]:
    """Every made tool on disk right now (project dir before config dir; the
    first definition of a name wins)."""
    out: list[ToolDef] = []
    seen: set[str] = set()
    for root in dirs(cwd):
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.py")):
            for tool in load_file(path):
                if tool.name not in seen:
                    seen.add(tool.name)
                    out.append(tool)
    return out


def refresh(session) -> bool:
    """Turn-start sync: did the made-tool files change on disk? Then the made
    layer of the tool list is rebuilt from disk (additions, replacements and
    deletions) — native/plugin/MCP tools are never touched, and made tools
    never shadow them. Returns True when the tool list changed."""
    fp = fingerprint(session.cwd)
    if _fingerprints.get(session.cwd) == fp:
        return False                              # nothing changed on disk: zero-cost no-op
    _fingerprints[session.cwd] = fp
    builtin = {t.name for t in session.tools} - {t.name for t in session.tools
                                                 if getattr(t, "made", False)}
    fresh = {t.name: t for t in discover(session.cwd) if t.name not in builtin}
    session.tools = ([t for t in session.tools if not getattr(t, "made", False)]
                     + list(fresh.values()))
    for t in fresh.values():
        t.made = True
    return True                                 # the files changed, so the tool list did too
