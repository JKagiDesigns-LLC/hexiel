#!/usr/bin/env python
"""How much text hexiel keeps out of the model's context for the two things an agent does first in any project:
get its bearings, and find-then-read one function. No model, no network, no cost — just hexiel's own tools
against a project on disk, so anyone can rerun it and get the same numbers.

    python bench/context_savings.py                # the hexiel source tree itself
    python bench/context_savings.py /path/to/repo  # any project
    python bench/context_savings.py --json         # machine-readable

Method (deliberately conservative toward the baseline, so the saving is not overstated):
  * Orientation. Baseline: the model reads every source file once to learn the layout (the upper bound; real
    agents read fewer, but then know less). hexiel: the `map` tool's output.
  * Find and read a function. For 25 functions picked evenly through the symbol index (>= 10 lines each), baseline:
    the model finds the file (grep, free) and reads the WHOLE file. hexiel: `where_is` + `read` of just that symbol.
  Tokens are estimated as characters / 4 (a common rule of thumb for English and code); the ratios do not depend on it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import symbols as sm                       # noqa: E402
from hexiel.tools import Toolchain, ToolContext, build_tools   # noqa: E402

SOURCE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".c", ".h", ".cpp", ".cs", ".rb",
              ".php", ".swift", ".scala", ".html", ".css", ".scss", ".sh"}


def toks(chars: int) -> int:
    return round(chars / 4)


def source_files(root: Path) -> list[Path]:
    out = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in sm.SKIP_DIRS and not d.startswith(".")]
        for f in files:
            p = Path(dirpath, f)
            if p.suffix in SOURCE_EXT:
                try:
                    if p.stat().st_size <= sm.MAX_FILE:
                        out.append(p)
                except OSError:
                    pass
    return out


def measure(root: Path, samples: int = 25) -> dict:
    tools = {t.name: t for t in build_tools(Toolchain())}
    ctx = ToolContext(cwd=str(root), chain=Toolchain())
    run = lambda tool, **a: tools[tool].run(a, ctx).text   # noqa: E731

    files = source_files(root)
    all_chars = sum(len(p.read_text("utf-8", "replace")) for p in files)
    map_chars = len(run("map"))

    sm._INDEXES.clear()
    idx = sm.index_for(str(root))
    funcs = sorted((s for s in idx.all() if s.kind in ("function", "method") and s.end - s.line >= 9),
                   key=lambda s: (s.path, s.line))
    step = max(1, len(funcs) // samples)
    picked = funcs[::step][:samples]
    rows = []
    for s in picked:
        whole = len((root / s.path).read_text("utf-8", "replace"))
        found = len(run("where_is", name=s.name))
        part = len(run("read", path=str(root / s.path), symbol=s.qualname))
        rows.append({"symbol": s.qualname, "file": s.path, "whole_file_chars": whole, "hexiel_chars": found + part})
    base = sum(r["whole_file_chars"] for r in rows)
    ours = sum(r["hexiel_chars"] for r in rows)
    return {
        "project": root.name, "source_files": len(files), "source_tokens": toks(all_chars),
        "orientation": {"baseline_tokens": toks(all_chars), "hexiel_tokens": toks(map_chars),
                        "reduction_pct": round(100 * (1 - map_chars / all_chars), 1) if all_chars else 0},
        "find_and_read": {"functions": len(rows), "baseline_tokens": toks(base), "hexiel_tokens": toks(ours),
                          "reduction_pct": round(100 * (1 - ours / base), 1) if base else 0},
        "rows": rows,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--samples", type=int, default=25)
    a = ap.parse_args()
    r = measure(Path(a.path).resolve(), a.samples)
    if a.json:
        print(json.dumps(r, indent=2))
        return
    o, f = r["orientation"], r["find_and_read"]
    print(f"{r['project']}: {r['source_files']} source files, ~{r['source_tokens']:,} tokens in total")
    print(f"  get your bearings   {o['baseline_tokens']:>9,} -> {o['hexiel_tokens']:>7,} tokens  (-{o['reduction_pct']}%)")
    print(f"  find+read {f['functions']} functions {f['baseline_tokens']:>7,} -> {f['hexiel_tokens']:>7,} tokens  (-{f['reduction_pct']}%)")


if __name__ == "__main__":
    main()
