#!/usr/bin/env python
"""Does hiding old tool results change what the model does next? An offline check on your own saved sessions.

`hexiel --audit` says masking old tool results would carry far fewer tokens. That number ignores the question that
matters: does the model still do the right thing once the old output is gone? This asks it. At sampled points in
real saved sessions (a model request that followed at least KEEP requests and big old tool results) it re-runs that
one request three ways and compares what the model chose to do next:

    full    the conversation exactly as it was                 (run twice: the model's own run-to-run noise)
    masked  every tool result older than KEEP requests swapped for a one-line stub

The first run (29 points, 5 sessions, keep 6, di-deepseek-flash) used a stub without the call's argument; the shipped
stub names the call too, so rerun it to measure exactly what `mask_old_results = "on"` does.

Agreement means the same tool and the same main argument (path, command, pattern, ...). The run-to-run agreement
of "full" against itself is the ceiling: masking can only be judged against it. Costs real tokens on the model you
pick; sessions are sent to that model (they already were, when you made them).

    python bench/mask_replay.py --model di-deepseek-flash --points 30
    python bench/mask_replay.py --dry-run            # which points would be used, and the token cost, no calls
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import audit, auth                     # noqa: E402
from hexiel.config import load_config              # noqa: E402
from hexiel.llm import make_provider               # noqa: E402
from hexiel.prompt import BASE_PROMPT              # noqa: E402
from hexiel.tooling import Toolchain               # noqa: E402
from hexiel.tools import build_tools               # noqa: E402

KEY_ARGS = ("path", "command", "pattern", "query", "name", "symbol", "target", "url", "id", "op", "action")


def stub(tool: str, args, tokens: int) -> str:
    from hexiel import masking
    return masking.stub(tool, args, tokens)      # the text hexiel itself would put there


def mask(messages: list[dict], keep: int) -> list[dict]:
    """Copy of `messages` with big tool results older than `keep` assistant requests replaced by a stub."""
    reqs = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
    names = {}
    for m in messages:
        for c in audit._calls(m):
            names[c.get("id", "")] = (c.get("name", "?"), c.get("arguments"))
    out = []
    for i, m in enumerate(messages):
        m = {k: v for k, v in m.items() if k != "ts"}
        if m.get("role") == "tool":
            t = audit.toks(m.get("content", ""))
            later = sum(1 for r in reqs if r > i)
            if t >= audit.MIN_TOKENS and later > keep:
                name, args = names.get(m.get("tool_call_id", ""), ("tool", None))
                m = {**m, "content": stub(name, args, t)}
        out.append(m)
    return out


def clean(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        m = {k: v for k, v in m.items() if k != "ts"}
        if isinstance(m.get("tool_calls"), str):
            m["tool_calls"] = audit._calls(m)
        out.append(m)
    return out


def signature(call: dict) -> tuple[str, str]:
    args = call.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    main = next((str(args[k]) for k in KEY_ARGS if k in args), "")
    return call.get("name", "?"), main.strip()[:200]


def points(files: list[Path], keep: int, n: int, seed: int) -> list[dict]:
    """Decision points where masking actually changes the prompt: >= keep earlier requests, a big old result."""
    found = []
    for f in files:
        msgs = audit.load(f)
        for i, m in enumerate(msgs):
            if m.get("role") != "assistant" or not audit._calls(m):
                continue
            before = msgs[:i]
            if not before or before[-1].get("role") not in ("tool", "user"):
                continue
            if mask(before, keep) == [{k: v for k, v in x.items() if k != "ts"} for x in before]:
                continue
            found.append({"file": f.name, "index": i, "messages": clean(before), "truth": audit._calls(m)})
    random.Random(seed).shuffle(found)
    per: Counter = Counter()
    picked = []
    for p in found:                                  # spread over sessions: at most 6 points from one
        if per[p["file"]] < 6:
            per[p["file"]] += 1
            picked.append(p)
        if len(picked) >= n:
            break
    return picked


def ask(provider, mc, settings, tool_defs, messages) -> list[tuple[str, str]]:
    final = None
    for event, *payload in provider.stream(
            messages, BASE_PROMPT, tool_defs, model=mc.model, max_output_tokens=2048, extra_body=mc.extra_body,
            timeout=180, echo_reasoning=mc.echo_reasoning, max_tokens_param=mc.max_tokens_param,
            keep_alive="", num_ctx=0, conversation_id="mask-replay"):
        if event == "assistant":
            final = payload[0]
    return [signature(c) for c in audit._calls(final or {})]


def hidden_calls(messages: list[dict], keep: int) -> set:
    """Signatures of the calls whose results the mask hides: asking for one of them again is a re-fetch."""
    reqs = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
    sigs = {c.get("id", ""): signature(c) for m in messages for c in audit._calls(m)}
    out = set()
    for i, m in enumerate(messages):
        if (m.get("role") == "tool" and audit.toks(m.get("content", "")) >= audit.MIN_TOKENS
                and sum(1 for r in reqs if r > i) > keep):
            out.add(sigs.get(m.get("tool_call_id", ""), ("?", "")))
    return out


def agree(a: list, b: list) -> tuple[bool, bool]:
    """(same tool, same tool and main argument) for the model's first call in each."""
    if not a or not b:
        return (not a and not b), (not a and not b)
    return a[0][0] == b[0][0], a[0] == b[0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="di-deepseek-flash")
    ap.add_argument("--points", type=int, default=30)
    ap.add_argument("--keep", type=int, default=audit.KEEP_RECENT)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", metavar="FILE")
    a = ap.parse_args()
    pts = points(audit.session_files(), a.keep, a.points, a.seed)
    full_t = sum(audit.toks(p["messages"]) for p in pts)
    masked_t = sum(audit.toks(mask(p["messages"], a.keep)) for p in pts)
    print(f"{len(pts)} decision points from {len({p['file'] for p in pts})} sessions; prompt tokens per point: "
          f"full {full_t // max(1, len(pts)):,} → masked {masked_t // max(1, len(pts)):,} "
          f"({100 * (1 - masked_t / max(1, full_t)):.0f}% fewer)")
    if a.dry_run or not pts:
        return
    auth.keys_into_env()
    cfg = load_config()
    mc = cfg.models[a.model]
    provider = make_provider(mc, cfg.settings)
    tool_defs = [{"name": t.name, "description": t.description, "parameters": t.parameters}
                 for t in build_tools(Toolchain())]

    def work(p):
        try:
            return {"file": p["file"], "index": p["index"], "truth": [signature(c) for c in p["truth"]],
                    "full1": ask(provider, mc, cfg.settings, tool_defs, p["messages"]),
                    "full2": ask(provider, mc, cfg.settings, tool_defs, p["messages"]),
                    "masked": ask(provider, mc, cfg.settings, tool_defs, mask(p["messages"], a.keep))}
        except Exception as e:   # noqa: BLE001 - one failed point must not lose the rest
            return {"file": p["file"], "index": p["index"], "error": str(e)[:200]}

    with ThreadPoolExecutor(4) as ex:
        rows = list(ex.map(work, pts))
    ok = [r for r in rows if "error" not in r]
    print(f"{len(ok)} points answered, {len(rows) - len(ok)} failed")

    def rate(x, y):
        pairs = [agree(r[x], r[y]) for r in ok]
        n = max(1, len(pairs))
        return sum(p[0] for p in pairs) / n, sum(p[1] for p in pairs) / n
    for label, x, y in (("full vs full again (the ceiling)", "full1", "full2"),
                        ("masked vs full", "masked", "full1"),
                        ("full vs what really happened", "full1", "truth"),
                        ("masked vs what really happened", "masked", "truth")):
        tool, exact = rate(x, y)
        print(f"  {label:<36} same tool {tool:>4.0%}   same tool+argument {exact:>4.0%}")
    refetched = sum(1 for p, r in zip(pts, rows) if "error" not in r and r["masked"]
                    and r["masked"][0] in hidden_calls(p["messages"], a.keep))
    refetched_full = sum(1 for p, r in zip(pts, rows) if "error" not in r and r["full1"]
                         and r["full1"][0] in hidden_calls(p["messages"], a.keep))
    print(f"  asked again for output the mask hid: masked {refetched} of {len(ok)} "
          f"(with the full history, the same call {refetched_full} of {len(ok)})")
    if a.json:
        Path(a.json).write_text(json.dumps(rows, indent=2, default=str))


if __name__ == "__main__":
    main()
