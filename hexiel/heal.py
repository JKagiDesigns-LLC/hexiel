"""Self-healing: capture hexiel's own failures as incident files, then /heal
walks the agent through reproducing, fixing, testing, and proposing an MR
so the repo owner can approve the fix in GitLab."""

import json
import time
from pathlib import Path

from . import __version__


def incidents_dir(cwd: str) -> Path:
    return Path(cwd) / ".hexiel" / "incidents"


def record_incident(cwd: str, kind: str, detail: str, traceback_text: str = "",
                    context: dict | None = None) -> Path | None:
    """Persist one failure report; returns the path or None if we cannot write."""
    try:
        directory = incidents_dir(cwd)
        directory.mkdir(parents=True, exist_ok=True)
        slug = f"{int(time.time() * 1000)}-{kind}"
        payload = {"time": time.time(), "hexiel_version": __version__, "kind": kind,
                   "detail": detail[:4000], "traceback": traceback_text[:8000],
                   "context": context or {}}
        path = directory / f"{slug}.json"
        path.write_text(json.dumps(payload, indent=2))
        return path
    except OSError:
        return None


def unhealed(cwd: str, limit: int = 10) -> list[dict]:
    directory = incidents_dir(cwd)
    try:
        files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime)[-limit:]
    except OSError:
        return []
    payloads = []
    for path in files:
        try:
            payloads.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return payloads


def heal_prompt(incidents: list[dict], workspace: str, description: str = "",
                python: str = "python") -> str:
    """The agent prompt for /heal. The agent fixes and tests hexiel in an
    isolated workspace; committing/sharing happens afterwards, in the review
    screen, only for the files the user approves."""
    lines = [f"You are SELF-HEALING hexiel {__version__} itself (not the user's project).",
             "",
             f"Work ONLY inside this isolated copy of hexiel's source: {workspace}",
             "It is your working directory. The user's project and their installed copy",
             "are untouched until they review and approve your changes.",
             "",
             "Steps:",
             "1. Reproduce the problem: read the failing code paths (paths below are",
             "   relative to the workspace; the package lives in hexiel/).",
             "2. Fix the ROOT CAUSE in the source — no workarounds, no unrelated edits,",
             "   keep the diff minimal and in the surrounding code style.",
             "3. Add or extend a test in tests/ that fails without your fix.",
             f"4. Run the suite: `{python} -m pytest -q` (install pytest into that",
             f"   interpreter with `{python} -m pip install pytest` if it is missing).",
             "   Iterate until green.",
             "5. Do NOT commit, push or open merge requests — when you finish, hexiel shows",
             "   the user a diff review where they decide what to keep or send upstream.",
             "6. End with a short summary: root cause, the fix, and the test you added.",
             ""]
    if description:
        lines += ["The user reported:", f"> {description}", ""]
    lines.append("Recorded incidents:")
    if not incidents and not description:
        lines.append("(none recorded — run the test suite and fix whatever is red)")
    elif not incidents:
        lines.append("(none — work from the user's report above)")
    for incident in incidents:
        lines.append(f"- kind={incident.get('kind')} (hexiel {incident.get('hexiel_version')}) "
                     f"detail={incident.get('detail', '')[:600]}")
        trace = incident.get("traceback", "")
        if trace:
            lines.append("  traceback:\n" + "\n".join(f"    {ln}" for ln in trace.splitlines()[-12:]))
    return "\n".join(lines)


def mark_healed(cwd: str) -> int:
    """Move current incident files to incidents/healed/ once a fix is kept or sent."""
    directory = incidents_dir(cwd)
    done = directory / "healed"
    moved = 0
    try:
        for path in directory.glob("*.json"):
            done.mkdir(parents=True, exist_ok=True)
            path.rename(done / path.name)
            moved += 1
    except OSError:
        pass
    return moved
