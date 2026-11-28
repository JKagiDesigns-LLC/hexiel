"""Review pack: what an edit changed, what it puts at risk, and what is still unverified, in one page.

For the person who has to read the change, not the model that wrote it. Facts only, computed locally from the git
diff and the symbol index: files by kind, the functions and classes touched, signatures that changed (with how many
callers that can break), changed code no test file mentions, and what the language checkers say about the touched
files. It never says a change is "good": it says what a reviewer should look at first. Paste it into a pull request.

    hexiel --review            # uncommitted changes against HEAD
    hexiel --review main       # everything since main
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import impact
from . import symbols as sm

DOC_EXT = {".md", ".rst", ".txt", ".adoc"}
CONFIG_EXT = {".toml", ".yml", ".yaml", ".json", ".ini", ".cfg", ".lock", ".env"}
MAX_ROWS = 12


def _git(cwd: str, *args: str, timeout: float = 20) -> str:
    try:
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def kind_of(path: str) -> str:
    if impact.is_test_file(path):
        return "tests"
    ext = Path(path).suffix.lower()
    if ext in DOC_EXT:
        return "docs"
    if ext in CONFIG_EXT or Path(path).name in {"Dockerfile", "Makefile", ".gitignore"}:
        return "config"
    return "source"


def numstat(cwd: str, base: str) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for line in _git(cwd, "diff", "--numstat", "--no-color", base).splitlines():
        a, _, rest = line.partition("\t")
        d, _, path = rest.partition("\t")
        if a.isdigit() and d.isdigit():
            out[path] = (int(a), int(d))
    return out


def old_symbols(cwd: str, base: str, path: str) -> dict[str, sm.Symbol]:
    text = _git(cwd, "show", f"{base}:{path}")
    if not text:
        return {}
    try:
        return {s.qualname: s for s in sm.parse(text, path)}
    except Exception:   # noqa: BLE001 - an unparsable old version just means "can't compare"
        return {}


def api_changes(idx: sm.Index, cwd: str, base: str, paths: list[str]) -> list[dict]:
    """Added, removed and re-signatured functions/classes in the changed files."""
    rows = []
    for path in paths:
        if kind_of(path) != "source" or not Path(cwd, path).is_file():
            continue
        before = old_symbols(cwd, base, path)
        now = {s.qualname: s for s in idx.outline(path)}
        keep = ("function", "method", "class", "type")
        for q, s in now.items():
            if s.kind not in keep or s.name.startswith("_"):
                continue
            o = before.get(q)
            if o is None:
                if before:
                    rows.append({"what": "added", "q": q, "path": path, "sig": s.signature})
            elif o.signature and s.signature and o.signature != s.signature:
                rows.append({"what": "signature", "q": q, "path": path, "old": o.signature, "sig": s.signature})
        for q, o in before.items():
            if q not in now and o.kind in keep and not o.name.startswith("_"):
                rows.append({"what": "removed", "q": q, "path": path, "sig": o.signature})
    return rows


def build(cwd: str, base: str = "HEAD", chain=None, with_checks: bool = True) -> str:
    stat = numstat(cwd, base)
    if not stat:
        return f"nothing to review: no changes against {base}"
    idx = sm.index_for(cwd)
    idx.refresh()
    groups: dict[str, list[str]] = {}
    for p in sorted(stat):
        groups.setdefault(kind_of(p), []).append(p)
    plus = sum(a for a, _ in stat.values())
    minus = sum(d for _, d in stat.values())
    out = [f"# Review pack  ({len(stat)} file(s), +{plus} −{minus}, against {base})", ""]
    for kind in ("source", "tests", "config", "docs"):
        if kind in groups:
            names = ", ".join(f"{p} (+{stat[p][0]} −{stat[p][1]})" for p in groups[kind][:MAX_ROWS])
            more = f" … {len(groups[kind]) - MAX_ROWS} more" if len(groups[kind]) > MAX_ROWS else ""
            out.append(f"- **{kind}**: {names}{more}")
    ranges = impact.changed_ranges(cwd, base)
    syms = [s for s in impact.changed_symbols(idx, ranges) if not impact.is_test_file(s.path)]
    api = api_changes(idx, cwd, base, groups.get("source", []))
    infos = {s.qualname: impact.radius(idx, s.qualname) for s in syms[:MAX_ROWS]}

    risky = []
    for a in api:
        if a["what"] in ("signature", "removed"):
            n = infos.get(a["q"], impact.radius(idx, a["q"]))["total_refs"] if a["what"] == "signature" else \
                len(sm.callers(idx, a["q"], 400))
            risky.append((n, a))
    if risky:
        out += ["", "## Look at first: the interface changed"]
        for n, a in sorted(risky, key=lambda t: -t[0]):
            verb = f"`{a['q']}` {a['old']} → {a['sig']}" if a["what"] == "signature" else f"`{a['q']}` removed"
            out.append(f"- {verb}  ({a['path']}) — {n} reference(s) elsewhere that may need updating")
    added = [a for a in api if a["what"] == "added"]
    if added:
        out += ["", "## New public code"] + [f"- `{a['q']}` ({a['path']})" for a in added[:MAX_ROWS]]

    if syms:
        out += ["", "## Changed code and what depends on it"]
        for s in syms[:MAX_ROWS]:
            i = infos[s.qualname]
            tests = f"{len(i['tests'])} test file(s)" if i["tests"] else "**no test file mentions it**"
            out.append(f"- `{s.qualname}` ({s.path}:{s.line}) — {i['total_refs']} reference(s), {tests}")
        if len(syms) > MAX_ROWS:
            out.append(f"- … {len(syms) - MAX_ROWS} more")
    touched_tests = set(groups.get("tests", []))
    run_these = set(touched_tests)
    for i in infos.values():
        run_these.update(i["tests"])
    unverified = [s for s in syms[:MAX_ROWS] if not infos[s.qualname]["tests"]]
    gaps = (["source changed but no test file changed in this diff"] if "source" in groups and not touched_tests else []) \
        + [f"no test file references `{s.qualname}`" for s in unverified]
    if gaps:
        out += ["", "## Unverified"] + [f"- {g}" for g in gaps]
    cmd = impact.test_command(chain, sorted(run_these))
    if cmd:
        out += ["", f"Run the affected tests: `{cmd}`"]

    if any(i["total_refs"] > 20 for i in infos.values()):
        out += ["", "_References are matched by name, so unrelated code with a common name may be counted; "
                "the `lsp` tool gives exact references for one symbol._"]
    if with_checks:
        from . import diagnostics as dg
        files = [p for p in groups.get("source", []) if Path(cwd, p).is_file()][:8]
        found: list = []
        for p in files:
            found += dg.check(Path(cwd, p), cwd, 15)
        errors = [d for d in found if d.severity == "error"]
        out += ["", "## Checker output on the touched files",
                (f"- {len(errors)} error(s), {len(found) - len(errors)} warning(s):" if found else
                 f"- clean ({dg.available(cwd).splitlines()[0] if dg.available(cwd) else 'no checker installed'})")]
        out += [f"  - {d.note(cwd)}" for d in found[:MAX_ROWS]]
    return "\n".join(out)
