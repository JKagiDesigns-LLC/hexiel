"""Blast radius: before (or after) changing code, who depends on it and which tests exercise it.

One local answer instead of the model exploring: the definition, the direct callers and references grouped by file
with the symbol they sit in, a second hop (who calls those callers), and the test files among them with a command to
run just those. Pure Python over the symbol index, no model and no network. When a language server is installed the
`lsp` tool gives exact references for one symbol; this answers the wider question "what else is affected?" in any
language, and `target = "changes"` reads the working-tree diff so a review sees what the edit really touches.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import symbols as sm

MAX_DIRECT = 25
MAX_SECOND = 15
MAX_TESTS = 20

_TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "e2e", "integration"}
_TEST_NAME = re.compile(r"(^test_|_test\.|\.test\.|\.spec\.|_spec\.|Test\.java$|Tests?\.cs$|^conftest\.py$)")


def is_test_file(rel: str) -> bool:
    p = Path(rel)
    return bool(_TEST_NAME.search(p.name)) or any(part.lower() in _TEST_DIRS for part in p.parts[:-1])


_REF = re.compile(r"^(?P<path>.+?):(?P<line>\d+)(?: \(in (?P<inside>[^)]*)\))?: (?P<text>.*)$")


def _refs(idx: sm.Index, name: str, limit: int) -> list[dict]:
    out = []
    for line in sm.callers(idx, name, limit=limit):
        m = _REF.match(line)
        if m:
            out.append({"path": m["path"], "line": int(m["line"]), "inside": m["inside"] or "", "text": m["text"]})
    return out


def radius(idx: sm.Index, name: str) -> dict:
    """Facts about one symbol name: where it is defined, who uses it (two hops), which of those are tests."""
    short = name.split(".")[-1]
    defs = [s for s in idx.all() if s.name == short and (s.qualname == name or "." not in name)]
    direct = _refs(idx, short, 400)
    by_inside: dict[str, dict] = {}
    for r in direct:
        key = f"{r['path']}::{r['inside']}" if r["inside"] else r["path"]
        by_inside.setdefault(key, r)
    second: list[dict] = []
    seen = {short}
    for r in list(by_inside.values())[:MAX_DIRECT]:
        inner = r["inside"].split(".")[-1]
        if not inner or inner in seen or is_test_file(r["path"]):
            continue
        seen.add(inner)
        for r2 in _refs(idx, inner, 12):
            if r2["inside"].split(".")[-1] != inner:
                second.append({**r2, "via": inner})
        if len(second) >= MAX_SECOND * 3:
            break
    tests = sorted({r["path"] for r in direct + second if is_test_file(r["path"])})
    files = sorted({r["path"] for r in direct if not is_test_file(r["path"])})
    return {"name": name, "defs": defs, "direct": list(by_inside.values()), "second": second,
            "tests": tests, "files": files, "total_refs": len(direct)}


def test_command(chain, tests: list[str]) -> str:
    """How to run only these test files with the project's runner ('' when unknown)."""
    if not tests:
        return ""
    runners = getattr(chain, "test_runners", None) or []
    label = (runners[0].label if runners else "").lower()
    files = " ".join(tests[:MAX_TESTS])
    if "pytest" in label or any(t.endswith(".py") for t in tests):
        return f"pytest {files}"
    if "vitest" in label:
        return f"npx vitest run {files}"
    if "jest" in label:
        return f"npx jest {files}"
    if all(t.endswith("_test.go") for t in tests):
        return "go test " + " ".join(sorted({"./" + str(Path(t).parent) for t in tests}))
    return ""


def render(info: dict, chain=None) -> str:
    lines = []
    for d in info["defs"][:4]:
        lines.append(f"{d.kind} {d.qualname}  {d.where()}")
    if not info["defs"]:
        lines.append(f"{info['name']}: no definition found in the index")
    n = info["total_refs"]
    if not n:
        lines.append("no references outside its definition: nothing else is affected (check dynamic use: strings, reflection, config)")
        return "\n".join(lines)
    lines.append(f"{n} reference(s) in {len(info['files']) + len([t for t in info['tests']])} file(s):")
    for r in info["direct"][:MAX_DIRECT]:
        where = f"  (in {r['inside']})" if r["inside"] else ""
        lines.append(f"  {r['path']}:{r['line']}{where}")
    if len(info["direct"]) > MAX_DIRECT:
        lines.append(f"  … {len(info['direct']) - MAX_DIRECT} more")
    if info["second"]:
        lines.append("second hop (who calls those):")
        for r in info["second"][:MAX_SECOND]:
            lines.append(f"  {r['path']}:{r['line']}  (in {r['inside'] or 'module'}, calls {r['via']})")
    if info["tests"]:
        lines.append(f"tests that touch it ({len(info['tests'])}): " + ", ".join(info["tests"][:MAX_TESTS]))
        cmd = test_command(chain, info["tests"])
        if cmd:
            lines.append(f"run only those: {cmd}")
    else:
        lines.append("no test file references it: a change here is unverified by the suite")
    return "\n".join(lines)


# ------------------------------------------------------------------ working-tree changes

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_ranges(cwd: str, base: str = "HEAD") -> dict[str, list[tuple[int, int]]]:
    """{path: [(first, last) new-file line ranges]} from `git diff -U0 base`; deleted files are skipped."""
    try:
        out = subprocess.run(["git", "-C", cwd, "diff", "-U0", "--no-color", base], capture_output=True,
                             text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    ranges: dict[str, list[tuple[int, int]]] = {}
    path = None
    for line in out.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif path and (m := _HUNK.match(line)):
            start, count = int(m[1]), int(m[2] if m[2] is not None else 1)
            ranges.setdefault(path, []).append((start, start + max(count, 1) - 1))
    return ranges


def changed_symbols(idx: sm.Index, ranges: dict[str, list[tuple[int, int]]]) -> list[sm.Symbol]:
    """The innermost function/method/class containing each changed range."""
    found: dict[tuple, sm.Symbol] = {}
    for path, spans in ranges.items():
        syms = [s for s in idx.outline(path) if s.kind in ("function", "method", "class", "type")]
        for a, b in spans:
            hit = [s for s in syms if s.line <= b and s.end >= a]
            for s in sorted(hit, key=lambda s: s.end - s.line)[:1]:
                found[(s.path, s.qualname)] = s
    return list(found.values())


def run(args: dict, cwd: str, chain=None) -> tuple[str, bool]:
    target = str(args.get("target", "")).strip()
    if not target:
        return "error: target is a symbol name, a file path, or 'changes' (the uncommitted edits)", True
    idx = sm.index_for(cwd)
    idx.refresh()
    if target.lower() == "changes":
        ranges = changed_ranges(cwd, str(args.get("base") or "HEAD"))
        if not ranges:
            return "no uncommitted changes against " + str(args.get("base") or "HEAD"), False
        syms = [s for s in changed_symbols(idx, ranges) if not is_test_file(s.path)]
        tests_changed = sorted(p for p in ranges if is_test_file(p))
        if not syms:
            return (f"{len(ranges)} file(s) changed, no function or class in them (config, docs or top-level code)."
                    + (f" Tests changed: {', '.join(tests_changed)}" if tests_changed else "")), False
        parts, tests = [], set(tests_changed)
        for s in syms[:8]:
            info = radius(idx, s.qualname)
            tests.update(info["tests"])
            parts.append(f"## {s.qualname} ({s.path}:{s.line})\n" + render(info, chain))
        cmd = test_command(chain, sorted(tests))
        tail = f"\nrun the affected tests: {cmd}" if cmd else ""
        more = f"\n… {len(syms) - 8} more changed symbol(s)" if len(syms) > 8 else ""
        return "\n\n".join(parts) + more + tail, False
    path = Path(cwd, target)
    if path.is_file():
        rel = idx.rel(str(path))
        names = [s.qualname for s in idx.outline(rel) if s.kind in ("function", "method", "class", "type")
                 and not s.name.startswith("_")][:10]
        if not names:
            return f"{rel}: no public functions or classes in the index", False
        return "\n\n".join(f"## {n}\n" + render(radius(idx, n), chain) for n in names), False
    return render(radius(idx, target), chain), False
