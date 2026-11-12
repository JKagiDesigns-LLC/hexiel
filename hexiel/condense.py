"""Shrinking command output without a model: the same facts in far fewer tokens.

- `structure(cmd, out)`: well-known commands (git status, pip list/freeze, ls -l) become a compact digest.
- `condense(text)`: long logs keep their shape — repeated lines collapse into one with a count ("× 412 similar lines"),
  error/warning lines are always kept with a little context, plus the head and the tail — instead of a blind cut
  in the middle that may throw away the one line that mattered.
Both are pure functions, a few milliseconds even on megabytes."""

from __future__ import annotations

import re

_VOLATILE = re.compile(r"0x[0-9a-fA-F]+|\b[0-9a-f]{7,40}\b|\d+(?:[.:,]\d+)*|\s+")
_ERRORISH = re.compile(r"\b(error|errors|failed|failure|fatal|exception|traceback|panic|denied|refused|cannot|can't|"
                       r"not found|no such|undefined|segfault|abort|critical|warning|warn|deprecated|E\d{3,4}|FAIL)\b",
                       re.I)


def _shape(line: str) -> str:
    return _VOLATILE.sub("#", line.strip())[:200]


def collapse_repeats(lines: list[str]) -> tuple[list[str], int]:
    """Runs of lines that differ only in numbers/hashes/whitespace (progress bars, download logs, per-item noise)
    become the first one plus a count. Returns (lines, removed)."""
    out: list[str] = []
    removed = 0
    i = 0
    while i < len(lines):
        shape = _shape(lines[i])
        j = i + 1
        while j < len(lines) and _shape(lines[j]) == shape:
            j += 1
        out.append(lines[i])
        run = j - i
        if run > 2:
            out.append(lines[j - 1])
            out.append(f"   … × {run - 2} similar lines removed")
            removed += run - 2
        elif run == 2:
            out.append(lines[i + 1])
        i = j
    return out, removed


def condense(text: str, limit: int = 12_000, head: int = 40, tail: int = 80) -> str:
    """`text` if it already fits; otherwise repeats collapsed, then head + every error-ish line (±2) + tail."""
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    lines, removed = collapse_repeats(lines)
    note = f"[condensed locally: {removed} repeated lines collapsed" if removed else "[condensed locally"
    if len("\n".join(lines)) <= limit:
        return "\n".join(lines) + f"\n{note}]"
    keep = set(range(min(head, len(lines)))) | set(range(max(0, len(lines) - tail), len(lines)))
    flagged = [i for i, ln in enumerate(lines) if _ERRORISH.search(ln)]
    for i in flagged[:120]:
        keep.update(range(max(0, i - 2), min(len(lines), i + 3)))
    out, last = [], -1
    for i in sorted(keep):
        if i != last + 1:
            out.append(f"   … {i - last - 1} lines removed …")
        out.append(lines[i][:2000])
        last = i
    body = "\n".join(out)
    if len(body) > limit * 1.5:                    # an error flood: keep the first errors and the tail
        body = body[: int(limit * 0.6)] + "\n   … (more) …\n" + body[-int(limit * 0.6):]
    dropped = len(lines) - len(keep)
    return body + f"\n{note}; {dropped} ordinary lines dropped, all {len(flagged)} error/warning lines kept" \
                  f"{' (first 120 with context)' if len(flagged) > 120 else ''}; re-run with a narrower command " \
                  "(grep, tail) for anything else]"


# ---------------------------------------------------------------- structured digests of common commands

def _git_status(out: str) -> str | None:
    if "On branch" not in out and "HEAD detached" not in out:
        return None
    branch = re.search(r"On branch (\S+)", out)
    track = re.search(r"Your branch is (.+?)\.\s*$", out, re.M)
    section, groups = "", {"staged": [], "modified": [], "untracked": [], "conflicts": []}
    for line in out.splitlines():
        if line.startswith("Changes to be committed"):
            section = "staged"
        elif line.startswith("Changes not staged"):
            section = "modified"
        elif line.startswith("Untracked files"):
            section = "untracked"
        elif line.startswith("Unmerged paths"):
            section = "conflicts"
        elif line.startswith("\t") and section:
            groups[section].append(re.sub(r"^\s*(?:new file|modified|deleted|renamed|both modified|typechange):\s*",
                                          lambda m: "" if "modified" in m.group(0) else m.group(0).strip()[0] + " ",
                                          line.strip()))
    parts = [f"branch {branch.group(1) if branch else '(detached)'}" + (f", {track.group(1)}" if track else "")]
    for name, files in groups.items():
        if files:
            shown = ", ".join(files[:40]) + (f", … {len(files) - 40} more" if len(files) > 40 else "")
            parts.append(f"{name} ({len(files)}): {shown}")
    if len(parts) == 1:
        parts.append("working tree clean")
    return "\n".join(parts) + "\n[git status, condensed: d = deleted, n = new file, r = renamed]"


def _pip_list(out: str) -> str | None:
    rows = []
    for line in out.splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-\[\]]+)(?:\s+|==)([0-9][^\s]*)", line.strip())
        if m:
            rows.append(f"{m.group(1)} {m.group(2)}")
    if len(rows) < 10:
        return None
    return f"{len(rows)} packages: " + ", ".join(rows)


def _ls_long(out: str) -> str | None:
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 8)
        if len(parts) == 9 and re.match(r"^[dl\-bcps][rwxsStT\-]{9}", parts[0]):
            name = parts[8] + ("/" if parts[0][0] == "d" else "")
            rows.append(name if parts[0][0] == "d" else f"{name} ({parts[4]})")
    if len(rows) < 15:
        return None
    return f"{len(rows)} entries: " + ", ".join(rows)


_STRUCT = [(re.compile(r"^\s*git(?:\s+-C\s+\S+)?\s+status\s*$"), _git_status),
           (re.compile(r"^\s*(?:\S*/)?(?:pip3?|python3?\s+-m\s+pip|uv\s+pip)\s+(?:list|freeze)\b"), _pip_list),
           (re.compile(r"^\s*ls\s+-[a-zA-Z]*l[a-zA-Z]*\b"), _ls_long)]


def structure(cmd: str, out: str, min_chars: int = 1200) -> str | None:
    """A compact digest for a known command's output, or None. Short output is left alone — nothing to save."""
    if len(out) < min_chars:
        return None
    for rx, fn in _STRUCT:
        if rx.search(cmd):
            try:
                return fn(out)
            except Exception:  # noqa: BLE001 - a digest is a bonus; the raw output is the fallback
                return None
    return None
