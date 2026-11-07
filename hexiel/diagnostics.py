"""Language-diagnostics tool (0.8.7): real type/lint feedback for edits, cheap and local.

Rather than speak the Language Server Protocol, the module uses the checkers a
project already has installed, on the exact file (or directory) touched:

  Python  pyright (type + syntax, JSON output) — the accuracy lift after edits
  TS/JS   tsc --noEmit when a tsconfig.json is present
  Go      go vet on the file's directory
  Rust    cargo check --message-format short

Every checker's output is normalized to (path, line, col, severity, message)
and capped, so the chat sees a compact `path:line: severity: message` list.
`available()` also powers the tool result when nothing can check a file.

After a write/edit, `post_edit()` appends a short note when the checker finds a
real problem (auto mode; `diagnostics = "off"` in config keeps it syntax-only).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

POST_EDIT_TIMEOUT = 20.0      # a slow first pyright run is better than a silent one... barely
MAX_LINES = 40
MAX_CHARS = 1600

CHECKER_HINT = {
    ".py": "pyright (npm i -g pyright) or ruff",
    ".ts": "typescript (npm i -g typescript)",
    ".tsx": "typescript (npm i -g typescript)",
    ".go": "go vet (the Go toolchain)",
    ".rs": "cargo check (Rust)",
}


@dataclass
class Diag:
    path: str
    line: int
    col: int
    severity: str          # error | warning | info
    message: str
    tool: str

    def note(self, root: str) -> str:
        try:
            rel = str(Path(self.path).relative_to(Path(root)))
        except ValueError:
            rel = self.path
        where = f"{rel}:{self.line}" + (f":{self.col}" if self.col else "")
        return f"{where}: {self.severity}: {self.message[:160]}"


def _run(cmd: list[str], cwd: str, timeout: float) -> subprocess.CompletedProcess | None:
    """cmd if its binary/path exists; None when the checker isn't installed."""
    if cmd and shutil.which(cmd[0]) is None:
        return None
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------- per-language checkers

def _pyright(path: Path, root: str, timeout: float) -> list[Diag]:
    proc = _run(["pyright", "--outputjson", str(path)], root, timeout)
    if proc is None or not proc.stdout.strip():
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    out = []
    for d in data.get("generalDiagnostics", []):
        at = d.get("range", {}).get("start", {})
        out.append(Diag(path=d.get("file", str(path)), line=int(at.get("line", 0)) + 1,
                        col=int(at.get("character", 0)) + 1,
                        severity=d.get("severity", "error"),
                        message=d.get("message", "").replace("\n", " "),
                        tool="pyright"))
    return out


def _ruff(path: Path, root: str, timeout: float) -> list[Diag]:
    proc = _run(["ruff", "check", "--output-format", "json", str(path)], root, timeout)
    if proc is None or not proc.stdout.strip():
        return []
    try:
        items = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    return [Diag(path=i["filename"], line=int(i["location"]["row"]), col=int(i["location"]["column"]),
                 severity="error" if "E9" in i.get("code", "") else "warning",
                 message=f"{i.get('code', 'lint')}: {i.get('message', '')}", tool="ruff")
            for i in items]


def _tsc(path: Path, root: str, timeout: float) -> list[Diag]:
    """TypeScript/JavaScript: project-wide tsc when a tsconfig.json is in scope."""
    project = path.parent
    while project != project.parent and not (project / "tsconfig.json").is_file():
        project = project.parent
    if not (project / "tsconfig.json").is_file():
        return []
    proc = _run(["tsc", "--noEmit", "--pretty", "false"], str(project), timeout)
    if proc is None:
        return []
    return _parse_tsc(proc.stdout + proc.stderr)


def _govet(path: Path, root: str, timeout: float) -> list[Diag]:
    proc = _run(["go", "vet", str(path.parent if path.is_file() else path)], root, timeout)
    if proc is None:
        return []
    src = (proc.stdout + proc.stderr).replace("# command-line-arguments\n", "")
    return _parse_colon_lines(src, "go vet", default_sev="error")


def _cargo_check(path: Path, root: str, timeout: float) -> list[Diag]:
    project = path.parent
    while project != project.parent and not (project / "Cargo.toml").is_file():
        project = project.parent
    if not (project / "Cargo.toml").is_file():
        return []
    proc = _run(["cargo", "check", "--message-format", "short"], str(project), timeout)
    if proc is None:
        return []
    # short format: src/main.rs:12:34: error[E0308]: mismatched types (+ rustc's plain-text diags)
    return _parse_colon_lines(proc.stdout + proc.stderr, "cargo check", default_sev="error")


_COLON = re.compile(r"^(?P<path>.+?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<rest>.*)$")
_SEV = re.compile(r"^(?P<sev>error|warning|note|)(?:\s+(?:TS\d+|E\d+|F\d+|W\d+))?\s*[: -]*(?P<msg>.*)$")
_TSC_COL = re.compile(r"^(?P<path>.+?):(?P<line>\d+):(?P<col>\d+)\s*-\s*(?P<rest>error|warning|note)?\s*"
                      r"(?:(?:TS\d+|E\d+|F\d+|W\d+))?\s*:?\s*(?P<msg>.*)$")


def _parse_colon_lines(text: str, tool: str, default_sev: str = "info") -> list[Diag]:
    """`path:line[:col]: [severity:] message` — go vet (no severity word = error) and
    cargo-check short (`path:12:34: error[E0308]: message`)."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        m = _COLON.match(line)
        if not m or not m.group("path").endswith((".go", ".rs", ".py")):
            continue
        sev = _SEV.match(m.group("rest"))
        if not sev or not (sev.group("sev") or sev.group("msg")):
            continue
        out.append(Diag(path=m.group("path"), line=int(m.group("line")), col=int(m.group("col") or 0),
                        severity=({"note": "info"}.get(sev.group("sev"), sev.group("sev"))) or default_sev,
                        message=(sev.group("msg")[:160] or line), tool=tool))
    return out


def _parse_tsc(text: str, tool: str = "tsc") -> list[Diag]:
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        m = _TSC_COL.match(line)
        if not m or not m.group("msg"):
            continue
        out.append(Diag(path=m.group("path"), line=int(m.group("line")), col=int(m.group("col")),
                        severity=("info" if m.group("rest") == "note" else m.group("rest")) or "error",
                        message=m.group("msg")[:160], tool=tool))
    return out


def _dedupe(diags: list[Diag]) -> list[Diag]:
    seen, out = set(), []
    for d in diags:
        key = (d.path, d.line, d.col, d.message[:80])
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


# ---------------------------------------------------------------- public surface

_FILE_CHECKS = {                     # suffix -> ordered list of (checker, applies)
    ".py": (_pyright, _ruff),
    ".ts": (_tsc,), ".tsx": (_tsc,), ".mjs": (_tsc,), ".cjs": (_tsc,),
    ".go": (_govet,),
    ".rs": (_cargo_check,),
}


def has_checker(path: Path) -> bool:
    return path.suffix.lower() in _FILE_CHECKS


def check(path: Path, root: str, timeout: float = POST_EDIT_TIMEOUT) -> list[Diag]:
    """Diagnostics for one file (or every file under a directory) — empty when clean or uncheckable."""
    if path.is_file():
        diags: list[Diag] = []
        for fn in _FILE_CHECKS.get(path.suffix.lower(), ()):
            diags += fn(path, root, timeout)
        return _dedupe(diags)
    # a directory: one repo-level run per available language still present in it
    want = {p.suffix.lower() for p in path.rglob("*") if p.is_file()}
    if ".py" in want:
        diags = []
        for py in list(path.rglob("*.py"))[:80]:       # cap: a directory check is on-demand anyway
            diags += _pyright(py, root, timeout)
        return _dedupe(diags)
    if {".ts", ".tsx"} & want:
        return _tsc(next(iter(sorted(path.rglob("*.ts*")))), root, timeout)
    if ".go" in want:
        return _govet(path, root, timeout)
    return []


def format_diag(diags: list[Diag], root: str) -> str:
    """The compact text the model (and the chat note) sees."""
    if not diags:
        return "no diagnostics"
    lines = [d.note(root) for d in diags[:MAX_LINES]]
    if len(diags) > MAX_LINES:
        lines.append(f"(+{len(diags) - MAX_LINES} more: run with a narrower path)")
    text = "\n".join(lines)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n(truncated)"
    return text


def post_edit(path: Path, root: str, enabled: bool = True) -> str:
    """Suffix note for a write/edit result; '' when disabled, uncheckable or clean."""
    if not enabled or not has_checker(path) or not path.is_file():
        return ""
    diags = [d for d in check(path, root) if d.severity in ("error", "warning")]
    if not diags:
        return ""                                       # clean: say nothing, keep edits snappy
    head = f"\n🩺 {len(diags)} diagnostic(s) in {path.name} ({diags[0].tool}) — fix now or note in the reply:"
    body = "\n".join("  " + d.note(root) for d in diags[:8])
    return head + "\n" + body[:MAX_CHARS]


def available(root: str) -> str:
    """Which of the known checkers are installed here (tool result context)."""
    where = {name: bool(shutil.which(name))
             for name in ("pyright", "ruff", "tsc", "go", "cargo")}
    have = [k for k, v in where.items() if v]
    missing = [f"{k} ({CHECKER_HINT[k + '.py'] if k == 'pyright' or k == 'ruff' else k})"
               for k, v in where.items() if not v]
    return ("installed checkers: " + (", ".join(have) or "none")
            + ("; missing: " + ", ".join(missing) if missing else ""))
