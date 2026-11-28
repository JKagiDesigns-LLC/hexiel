"""Observation masking: old, large tool results are replaced by a one-line stub so they stop being carried.

Every request re-reads the whole conversation, so a 2,000-token `read` result made twenty requests ago has been paid
for twenty times. The model rarely needs it again, and when it does it can call the tool again. Off by default
(`mask_old_results = "on"` in config.toml); `hexiel --audit` shows what it would save on your own sessions.

Two things keep it safe and cheap:
  * It waits until `batch` results have aged past `keep` requests and masks them together. Changing old history
    invalidates the provider's prompt cache from that point, so doing it rarely (not every request) keeps the
    cached prefix stable in between.
  * It never touches small results, a loaded skill's instructions, or the todo list.
"""

from __future__ import annotations

import json

KEEP = 10                   # requests a result stays in full
BATCH = 8                   # old results that must pile up before any are masked
MIN_TOKENS = 200            # smaller results are not worth a stub
KEEP_TOOLS = {"skill", "todo", "load_tools", "mcp_search"}     # their output is instructions, not data
MARK = "[older "
KEY_ARGS = ("path", "command", "pattern", "query", "name", "symbol", "target", "url", "id", "op", "action")


def _toks(text) -> int:
    return round(len(text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)) / 4)


def _calls(msg: dict) -> list[dict]:
    raw = msg.get("tool_calls") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return [c for c in raw if isinstance(c, dict)]


def stub(tool: str, args, tokens: int) -> str:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    main = next((str(args[k]) for k in KEY_ARGS if isinstance(args, dict) and k in args), "")
    what = f"{tool} {main[:80]!r}" if main else tool
    return f"{MARK}{what} result removed to save space ({tokens} tokens); call {tool} again if you need it]"


def eligible(messages: list[dict], keep: int = KEEP, min_tokens: int = MIN_TOKENS) -> list[tuple[int, str, object, int]]:
    """(index, tool, args, tokens) of tool results that are big, old enough and not yet masked."""
    reqs = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
    calls = {c.get("id", ""): (c.get("name", "tool"), c.get("arguments")) for m in messages for c in _calls(m)}
    out = []
    for i, m in enumerate(messages):
        if m.get("role") != "tool" or m.get("images"):
            continue
        text = m.get("content")
        if not isinstance(text, str) or text.startswith(MARK) or text.startswith("[prior tool output elided"):
            continue
        name, args = calls.get(m.get("tool_call_id", ""), ("tool", None))
        t = _toks(text)
        if name in KEEP_TOOLS or t < min_tokens:
            continue
        if sum(1 for r in reqs if r > i) > keep:
            out.append((i, name, args, t))
    return out


def mask_in_place(messages: list[dict], keep: int = KEEP, batch: int = BATCH, min_tokens: int = MIN_TOKENS) -> int:
    """Mask the old big results once `batch` of them are waiting. Returns how many were masked (0 = waiting)."""
    todo = eligible(messages, keep, min_tokens)
    if len(todo) < max(1, batch):
        return 0
    for i, name, args, t in todo:
        messages[i]["content"] = stub(name, args, t)
    return len(todo)
