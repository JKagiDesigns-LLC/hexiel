"""Formatter + AST-aware editing (0.8.7).

`formatter_for(path)` picks the formatter a project already has installed —
ruff (python), prettier (ts/tsx/js/jsx/css/html/json), gofmt (go), rustfmt (rs) —
and `format_file()` runs it on one file when the project does not say `off`.
write/edit call it right after a successful edit, so the file on disk is what
`ruff format` would ship (a `format: false` argument opts a single call out).

AST-aware editing: `fuzzy_span()` recovers an edit's anchor when the exact
string missed because of whitespace — matching on a whitespace-normalized copy
of the file, replacing the true span with the new text once and reporting how
it matched, instead of erroring and forcing a re-read loop.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

FORMAT_TIMEOUT = 30.0

CHECKERS = (                      # suffix -> (name, args-builder); first installed one wins
    ((".py",), "ruff", lambda f: ["ruff", "format", str(f)], None),
    ((".py",), "black", lambda f: ["black", "--quiet", str(f)], None),
    ((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css", ".scss", ".html", ".json"),
     "prettier", lambda f: ["prettier", "--write", "--log-level", "error", str(f)], None),
    ((".go",), "gofmt", lambda f: ["gofmt", "-w", "-s", str(f)], None),
    ((".rs",), "rustfmt", lambda f: ["rustfmt", "--quiet", str(f)], None),
)


def formatter_for(path: Path) -> tuple[str, list[str]] | None:
    """(name, cmd) for this file type, or None when nothing applies/is installed."""
    suffix = path.suffix.lower()
    for suffixes, name, build, _unused in CHECKERS:
        if suffix in suffixes and shutil.which(name):
            return name, [a.replace("\\", "/") for a in build(str(path))]
    return None


def format_file(path: Path, timeout: float = FORMAT_TIMEOUT) -> str:
    """Run the file's formatter. Returns a note (''), or when nothing applies '' as well —
    the caller only surfaces actual formatting. Never raises: a formatter failing does
    not fail the edit that already happened."""
    got = formatter_for(path)
    if got is None:
        return ""
    name, cmd = got
    try:
        before = path.read_text()
    except OSError:
        return ""
    try:
        proc = subprocess.run(cmd, cwd=str(path.parent), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    after = path.read_text() if path.is_file() else before       # a crash or a delete: leave it alone
    if after != before:
        return f"\n✨ formatted with {name}"
    if proc.returncode != 0:
        return f"\n✨ formatter ran with a warning: {(proc.stdout + proc.stderr).strip()[-300:]}"
    return ""

# ---------------------------------------------------------------- AST-aware editing


_WS = re.compile(r"[ \t]+")


def normalize(text: str) -> str:
    """A canonical copy for anchor matching: tabs/space runs collapse, trailing blanks drop,
    common indentation is preserved as a relative shape (never as noise)."""
    lines = [_WS.sub(" ", line.rstrip()).strip() for line in text.splitlines()]
    return "\n".join(lines)


def fuzzy_span(text: str, old: str) -> tuple[int, int] | None:
    """Where `old` lives in `text` when the exact string missed but a whitespace-equivalent
    version exists (indentation drift, tabs vs spaces, trailing gaps). None when absent or
    ambiguous — ambiguity still needs the caller's uniqueness error, not a guess."""
    if not old.strip():
        return None
    want = normalize(old)
    if not want:
        return None
    # slide a window over the file's lines, matching the normalized old against the
    # same number of file lines (old may span multiple lines with blank lines between)
    old_lines = want.split("\n")
    n = len(old_lines)
    if not n:
        return None
    lines = text.split("\n")
    hits = []
    for i in range(0, len(lines) - n + 1):
        window = normalize("\n".join(lines[i:i + n]))
        if window == want:
            start = sum(len(l) + 1 for l in lines[:i])
            end = start + sum(len(l) + 1 for l in lines[i:i + n]) - 1
            hits.append((start, min(end, len(text))))
            if len(hits) > 1:
                return None                                       # ambiguous: caller keeps its error
    return hits[0] if hits else None
