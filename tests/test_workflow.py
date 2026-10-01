"""Undo checkpoints, custom slash commands, usage totals."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import commands as C
from hexiel.config import ModelConfig, Settings
from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, build_tools


def session(tmp_path):
    tools = [t for t in build_tools(Toolchain()) if t.name in ("write", "edit", "read")]
    s = Session(ModelConfig(name="t", provider="openai", model="m", base_url="http://x"),
                Settings(), str(tmp_path), tools, "sys")
    s.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    s.auto_approve = True
    return s


def test_undo_reverts_last_turn_only(tmp_path):
    s = session(tmp_path)
    (tmp_path / "a.txt").write_text("v1\n")
    s.checkpoints.append([])                                   # turn 1
    s._execute_tool({"id": "1", "name": "write", "arguments": {"path": "a.txt", "content": "v2\n"}})
    s.checkpoints.append([])                                   # turn 2
    s._execute_tool({"id": "2", "name": "write", "arguments": {"path": "a.txt", "content": "v3\n"}})
    s._execute_tool({"id": "3", "name": "write", "arguments": {"path": "new.txt", "content": "x"}})
    s._execute_tool({"id": "4", "name": "write", "arguments": {"path": "a.txt", "content": "v4\n"}})
    restored = s.undo()
    assert (tmp_path / "a.txt").read_text() == "v2\n" and not (tmp_path / "new.txt").exists()
    assert len(restored) == 2
    s.undo()
    assert (tmp_path / "a.txt").read_text() == "v1\n"
    assert s.undo() == []


def test_custom_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cdir = tmp_path / ".claude" / "commands" / "git"
    cdir.mkdir(parents=True)
    (cdir / "review.md").write_text("---\ndescription: Review staged changes\nargument-hint: [focus]\n---\n"
                                    "Review `git diff --cached`, focusing on $ARGUMENTS. First word: $1.\n")
    (tmp_path / ".hexiel" / "commands").mkdir(parents=True)
    (tmp_path / ".hexiel" / "commands" / "fix.md").write_text("Fix the failing tests.\n")
    found = C.discover(str(tmp_path))
    assert set(found) == {"git:review", "fix"}
    assert found["git:review"].description == "Review staged changes"
    assert found["git:review"].render("error handling") == \
        "Review `git diff --cached`, focusing on error handling. First word: error."
    assert found["fix"].render("in auth.py") == "Fix the failing tests.\n\nin auth.py"


def test_usage_totals_accumulate(tmp_path, monkeypatch):
    s = session(tmp_path)

    class P:
        def stream(self, *a, **k):
            yield ("assistant", {"role": "assistant", "content": "hi"})
            yield ("done", {"prompt_tokens": 1000, "cached_tokens": 800, "completion_tokens": 20})
    s._stream_once(P(), [])
    s._stream_once(P(), [])
    assert s.usage_totals == {"requests": 2, "prompt_tokens": 2000, "cached_tokens": 1600,
                              "completion_tokens": 40}
