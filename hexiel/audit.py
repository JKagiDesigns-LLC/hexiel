"""Token audit: where a session's context went, and what trimming it would have saved, measured on your own sessions.

Every model request re-reads the whole conversation, so a tool result costs its size times the number of requests
made after it: its *carry cost*. This reads saved sessions (no model, no network) and reports, by tool:
  * what each tool's results weigh, and what they cost to carry;
  * outputs fetched more than once with an identical result (pure repeat);
  * a replay of "mask old tool results": every result older than KEEP_RECENT requests is swapped for a one-line
    stub. The replay charges a miss whenever the model asked for the same thing again after it was masked, so the
    saving it prints is net of the work masking would have cost.
Tokens are characters / 4, the same rule of thumb as bench/context_savings.py; ratios do not depend on it.

    hexiel --audit                 # every saved session
    hexiel --audit path/to/session.json ...
"""

from __future__ import annotations

import json
from pathlib import Path

KEEP_RECENT = 10          # requests a result stays in full before the replay masks it
STUB_TOKENS = 25          # a masked result becomes one short line
MIN_TOKENS = 200          # smaller results are never worth masking


def toks(text) -> int:
    return round(len(text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)) / 4)


def _calls(msg: dict) -> list[dict]:
    raw = msg.get("tool_calls") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return [c for c in raw if isinstance(c, dict)]


def _key(call: dict) -> str:
    args = call.get("arguments", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            pass
    return call.get("name", "?") + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)


def analyze(messages: list[dict], keep_recent: int = KEEP_RECENT) -> dict:
    """Numbers for one conversation."""
    requests = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
    req_after = {i: sum(1 for r in requests if r > i) for i in range(len(messages))}
    names: dict[str, tuple[str, str]] = {}
    for m in messages:
        for c in _calls(m):
            names[c.get("id", "")] = (c.get("name", "?"), _key(c))
    by_tool: dict[str, dict] = {}
    seen: dict[str, str] = {}
    repeats = repeat_tokens = 0
    carry = masked_carry = refetch = masked_n = 0
    masked_at: dict[str, int] = {}
    req_index = {r: n for n, r in enumerate(requests)}
    for i, m in enumerate(messages):
        if m.get("role") != "tool":
            continue
        name, key = names.get(m.get("tool_call_id", ""), ("?", "?"))
        t = toks(m.get("content", ""))
        row = by_tool.setdefault(name, {"calls": 0, "tokens": 0, "carry": 0})
        row["calls"] += 1
        row["tokens"] += t
        c = t * req_after[i]
        row["carry"] += c
        carry += c
        text = str(m.get("content", ""))
        if key in seen and seen[key] == text and t >= MIN_TOKENS:
            repeats += 1
            repeat_tokens += t
        seen[key] = text
        if key in masked_at and t >= MIN_TOKENS:
            refetch += t                       # asked again after the replay hid it: pay for it once more
            masked_at.pop(key, None)
        # replay: this result stays in full for `keep_recent` requests, then is a stub
        later = [r for r in requests if r > i]
        full_for = min(len(later), keep_recent)
        if t >= MIN_TOKENS and len(later) > keep_recent:
            masked_carry += t * full_for + STUB_TOKENS * (len(later) - full_for)
            masked_n += 1
            masked_at[key] = later[keep_recent]
        else:
            masked_carry += c
    saved = carry - masked_carry - refetch
    return {"requests": len(requests), "tool_results": sum(r["calls"] for r in by_tool.values()), "by_tool": by_tool,
            "repeats": repeats, "repeat_tokens": repeat_tokens, "carry": carry, "masked_carry": masked_carry,
            "refetch": refetch, "masked": masked_n, "saved": saved}


def merge(parts: list[dict]) -> dict:
    out = {"requests": 0, "tool_results": 0, "by_tool": {}, "repeats": 0, "repeat_tokens": 0, "carry": 0,
           "masked_carry": 0, "refetch": 0, "masked": 0, "saved": 0}
    for p in parts:
        for k in ("requests", "tool_results", "repeats", "repeat_tokens", "carry", "masked_carry", "refetch", "masked", "saved"):
            out[k] += p[k]
        for n, r in p["by_tool"].items():
            row = out["by_tool"].setdefault(n, {"calls": 0, "tokens": 0, "carry": 0})
            for k in row:
                row[k] += r[k]
    return out


def load(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return []
    msgs = data.get("messages") if isinstance(data, dict) else None
    return [m for m in msgs if isinstance(m, dict)] if isinstance(msgs, list) else []


def session_files() -> list[Path]:
    from .config import home_data_dir
    return sorted((home_data_dir() / "sessions").glob("*.json"))


def render(total: dict, sessions: int) -> str:
    if not total["tool_results"]:
        return f"{sessions} session(s) read: no tool results to audit"
    n = lambda x: f"{x:,}"  # noqa: E731
    lines = [f"Token audit — {sessions} session(s), {n(total['requests'])} model requests, "
             f"{n(total['tool_results'])} tool results", ""]
    lines.append(f"{'tool':<14}{'results':>8}{'tokens':>10}{'carried':>13}  share of carried")
    rows = sorted(total["by_tool"].items(), key=lambda kv: -kv[1]["carry"])
    tot = total["carry"] or 1
    for name, r in rows[:8]:
        lines.append(f"{name:<14}{n(r['calls']):>8}{n(r['tokens']):>10}{n(r['carry']):>13}  {100 * r['carry'] / tot:>4.0f}%")
    lines += ["", f"carried = tokens × the requests made after them: {n(total['carry'])} token-reads in total.",
              f"repeats: {total['repeats']} result(s) fetched again with identical output, {n(total['repeat_tokens'])} tokens "
              "(the result cache and `read` stale-guard already serve many of these)."]
    pct = 100 * total["saved"] / tot
    lines += ["", f"Replay — mask results older than {KEEP_RECENT} requests ({n(total['masked'])} results of {MIN_TOKENS}+ tokens):",
              f"  carried {n(total['carry'])} → {n(total['masked_carry'] + total['refetch'])} token-reads "
              f"(net {n(total['saved'])}, {pct:.0f}% less), including {n(total['refetch'])} tokens to fetch again what "
              "was hidden and then asked for.",
              "  Treat this as an UPPER BOUND, not a forecast: it only charges a miss when the model repeats the "
              "identical call, but a model that lost a detail more often acts on a wrong guess. Cached input is "
              "billed at a fraction of full price, so the dollar saving is smaller than the token saving; it is "
              "largest on providers without a cache and on long sessions."]
    return "\n".join(lines)


def main(paths: list[str]) -> str:
    files = [Path(p) for p in paths] or session_files()
    parts = [analyze(m) for m in (load(f) for f in files) if m]
    return render(merge(parts), len(parts))
