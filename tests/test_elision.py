"""Outdated-read elision: when a file is read again later in the history, the earlier read's file
body shrinks to a one-line stamp — batched to >= 64 KB of reclaim so prompt caches survive."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel.session import Session
from hexiel.tools import ToolResult
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext


def read_result(i: int, path: str, body: str, is_error: bool = False, call_id: str | None = None) -> tuple:
    """A read tool call (in an assistant entry) + its result entry, ready to append in order."""
    call = {"id": call_id or f"c{i}", "name": "read", "arguments": {"path": path}}
    assistant = {"role": "assistant", "content": "", "tool_calls": [call]}
    result = {"role": "tool", "tool_call_id": call["id"], "content": f"{path}\n{body}"}
    if is_error:
        result["is_error"] = True
    return assistant, result


def fresh(tmp_path, big: int = 90_000):
    s = Session.__new__(Session)                # bypass __init__: this suite only exercises the method
    s.mirrors = []                              # on_event/on_approve are properties over these holders
    s.messages = []
    s.save = lambda: None
    events = []
    s.on_event = lambda e, *p, **k: events.append((e, *p))
    body = "x = 1\n" * (big // 6)
    for n, path in enumerate(("a.py", "a.py", "b.py")):
        call, result = read_result(n, path, body, call_id=f"c{n + 1}")
        s.messages.extend((call, result))
    return s, events


def test_superseded_reads_shrink_and_the_newest_survives(tmp_path):
    s, events = fresh(tmp_path)
    s._supersede_reads()
    first, second, third = [m for m in s.messages if m["role"] == "tool"]
    assert "[outdated" in first["content"] and first["content"].startswith("a.py")
    assert "x = 1" in second["content"] and "[outdated" not in second["content"]   # the newest read stands
    assert "x = 1" in third["content"] and "[outdated" not in third["content"]
    assert any("superseded" in str(e[1]) for e in events if e[0] == "note")


def test_below_the_batch_threshold_nothing_is_rewritten(tmp_path):
    s, events = fresh(tmp_path, big=4_000)                  # ~4 KB of reclaim < 64 KB floor
    s._supersede_reads()
    tools = [m for m in s.messages if m["role"] == "tool"]
    assert all("x = 1" in m["content"] for m in tools)      # history untouched


def test_errors_and_lone_reads_are_never_elided(tmp_path):
    s = Session.__new__(Session)
    s.mirrors = []
    s.messages = []
    s.save = lambda: None
    s.on_event = lambda e, *p, **k: None
    body = "z\n" * 20_000
    err_call, err_result = read_result(0, "e.py", body, is_error=True)   # failed read: not a supersession
    lone_call, lone_result = read_result(1, "only.py", body)              # read once, never again
    s.messages.extend((err_call, err_result, lone_call, lone_result))
    s._supersede_reads()
    tools = [m for m in s.messages if m["role"] == "tool"]
    assert all("[outdated" not in m["content"] for m in tools)


def test_the_threshold_is_64k(tmp_path):
    assert Session.SUPERSEDE_MIN_CHARS == 64 * 1024