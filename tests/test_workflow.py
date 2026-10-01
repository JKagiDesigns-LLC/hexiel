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


def _hook_settings(tmp_path, hooks):
    (tmp_path / ".claude").mkdir(exist_ok=True)
    (tmp_path / ".claude" / "settings.json").write_text(__import__("json").dumps({"hooks": hooks}))


def test_pre_tool_hook_blocks_and_post_hook_feeds_back(tmp_path):
    _hook_settings(tmp_path, {
        "PreToolUse": [{"matcher": "Write", "hooks": [{"type": "command",
            "command": "python3 -c \"import json,sys; d=json.load(sys.stdin); p=d['tool_input']['file_path']; "
                       "sys.exit(2 if p.endswith('.env') else 0)\" || { echo 'no .env writes' >&2; exit 2; }"}]}],
        "PostToolUse": [{"matcher": "Edit|Write", "hooks": [{"type": "command",
            "command": "echo 'lint: line 1 too long' >&2; exit 2"}]}]})
    s = session(tmp_path)
    s.checkpoints.append([])
    s._execute_tool({"id": "1", "name": "write", "arguments": {"path": ".env", "content": "SECRET=1"}})
    assert not (tmp_path / ".env").exists() and "PreToolUse hook blocked" in s.messages[-1]["content"]
    assert "no .env writes" in s.messages[-1]["content"]
    s._execute_tool({"id": "2", "name": "write", "arguments": {"path": "a.py", "content": "x = 1\n"}})
    assert (tmp_path / "a.py").exists() and "lint: line 1 too long" in s.messages[-1]["content"]


def test_runaway_output_guard():
    from hexiel.session import _degenerate
    assert _degenerate("ok " + "#!" * 120)
    assert not _degenerate("def f():\n    return 1\n" * 20)
    assert not _degenerate("=" * 30)          # a short separator line is fine


def test_syntax_check_after_write(tmp_path):
    s = session(tmp_path)
    s.checkpoints.append([])
    s._execute_tool({"id": "1", "name": "write", "arguments": {"path": "ok.py", "content": "x = 1\n"}})
    assert "syntax check" not in s.messages[-1]["content"]
    s._execute_tool({"id": "2", "name": "write", "arguments": {"path": "bad.py", "content": "def f(:\n"}})
    assert "syntax check failed" in s.messages[-1]["content"] and "line 1" in s.messages[-1]["content"]
    s._execute_tool({"id": "3", "name": "write", "arguments": {"path": "c.json", "content": "{\"a\": }"}})
    assert "syntax check failed" in s.messages[-1]["content"]


def test_subagent_returns_only_its_answer(tmp_path, monkeypatch):
    s = session(tmp_path)
    (tmp_path / "big.txt").write_text("needle at line 3\n" * 200)
    replies = iter([
        {"role": "assistant", "content": "", "tool_calls": [{"id": "r1", "name": "read", "arguments": {"path": "big.txt"}}]},
        {"role": "assistant", "content": "The needle is in big.txt, line 3."}])
    from hexiel.session import Session as S
    monkeypatch.setattr(S, "_stream_assistant", lambda self, provider, defs: next(replies))
    monkeypatch.setattr("hexiel.llm.make_provider", lambda *a, **k: None)
    events = []
    s.on_event = lambda e, *p, **k: events.append(e)
    result = s.run_subagent({"description": "find needle", "prompt": "where is the needle?"})
    assert "big.txt, line 3" in result.text and "1 tool call" in result.text
    assert "needle at line 3\nneedle" not in result.text            # raw file content stays in the helper
    assert s.messages == [] and "subagent" in events
