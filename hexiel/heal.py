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


def heal_prompt(cwd: str) -> str:
    """The agent prompt /heal runs: reproduce, fix on a branch, test, propose MR."""
    incidents = unhealed(cwd)
    lines = [f"You are performing a SELF-HEAL of hexiel {__version__} itself.",
             "",
             "Recorded incident reports follow. Your job:",
             "1. `todo` create a heal plan (reproduce, fix, verify, propose).",
             "2. Reproduce each incident; read the failing code paths carefully.",
             "3. Fix the root cause in hexiel's source (NOT a workaround around it).",
             "4. Run the test suite with the `test` tool; iterate until green.",
             "5. Commit on a branch named hexiel-fix/<short-slug> and propose your",
             "   changes with the gitlab_mr tool (action=propose) so the maintainer",
             "   can review and approve. NEVER push to main; NEVER merge.",
             "6. Update memory with any structural lesson, then write the MR result.",
             "",
             "Incidents:"]
    if not incidents:
        lines.append("(none recorded — run the test suite and fix whatever is red)")
    for incident in incidents:
        lines.append(f"- kind={incident.get('kind')} detail={incident.get('detail', '')[:600]}")
        trace = incident.get("traceback", "")
        if trace:
            lines.append("  traceback:\n" + "\n".join(f"    {ln}" for ln in trace.splitlines()[-12:]))
    version = Path(__file__).parent.resolve()
    lines.append(f"\nhexiel source root: {version}")
    return "\n".join(lines)