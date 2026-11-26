"""Shared findings: what helpers (task subagents, team members) have learned, readable by every other helper.

Each helper normally works in a sealed context and only its final answer reaches the lead, so parallel helpers redo
each other's searches and a helper started later knows nothing the earlier ones found. One Findings board hangs off
the lead's session and is shared with every helper it starts (the whole tree, not just one fan-out):

- a helper finishing posts its answer here automatically, so later helpers start from it;
- a running helper can `share_finding` something the moment it learns it and `read_findings` what others have posted;
- a new helper is told, up front, what is already known (a capped digest) so it does not repeat that work.

Thread-safe; entries are small and the digest is capped, so this costs little context."""

from __future__ import annotations

import threading
import time

ENTRY_CAP = 1500          # one posted finding, in characters
DIGEST_CAP = 3500         # what a new helper is told up front


class Findings:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.entries: list[dict] = []          # {n, author, topic, text, time}

    def post(self, author: str, topic: str, text: str) -> str:
        text = " ".join(str(text).split()) if len(str(text)) < 200 else str(text).strip()
        if not text:
            return "error: nothing to share"
        topic = " ".join(str(topic or "").split())[:80] or "finding"
        with self.lock:
            for e in self.entries:                       # the same finding twice (a retry) adds nothing
                if e["author"] == author and e["text"] == text[:ENTRY_CAP]:
                    return f"already shared as #{e['n']}"
            n = len(self.entries) + 1
            self.entries.append({"n": n, "author": author, "topic": topic, "text": text[:ENTRY_CAP],
                                 "time": time.time()})
        return f"shared as #{n}"

    def render(self, since: int = 0, exclude: str = "", cap: int = DIGEST_CAP) -> str:
        """Entries after #since (not by `exclude`), newest kept when the cap bites."""
        with self.lock:
            rows = [e for e in self.entries if e["n"] > since and e["author"] != exclude]
        if not rows:
            return ""
        lines = [f"#{e['n']} [{e['author']}] {e['topic']}: {e['text']}" for e in rows]
        out, used = [], 0
        for line in reversed(lines):
            if used + len(line) > cap and out:
                out.append(f"… {len(lines) - len(out)} earlier finding(s) omitted — read_findings shows them all")
                break
            out.append(line)
            used += len(line)
        return "\n".join(reversed(out))

    def __len__(self) -> int:
        with self.lock:
            return len(self.entries)


def tools_for(board: Findings, author: str):
    """The two tools a helper gets, bound to its own name so entries say who found what."""
    from .tools import ToolDef, text_result

    def share(a: dict, ctx):
        return text_result(board.post(author, a.get("topic", ""), a.get("text", "")))

    def read(a: dict, ctx):
        try:
            since = int(a.get("since") or 0)
        except (TypeError, ValueError):
            since = 0
        out = board.render(since=since, cap=12_000)
        return text_result(out or "no findings from other helpers yet")

    return [
        ToolDef("share_finding",
                "Share a fact you just established (a file path and what it does, a config value, a dead end) with "
                "the other helpers working on this chat, so they do not redo it. Keep it short and concrete.",
                {"type": "object", "properties": {"topic": {"type": "string", "description": "2-6 words"},
                                                  "text": {"type": "string", "description": "the finding, with paths"}},
                 "required": ["text"]},
                readonly=True, always_ok=True, run=share),
        ToolDef("read_findings",
                "See what the other helpers on this chat have already found (and posted). Check it before a broad "
                "search; since=N shows only entries after #N.",
                {"type": "object", "properties": {"since": {"type": "integer"}}},
                readonly=True, always_ok=True, run=read),
    ]
