"""Permission rules (Claude Code syntax), scoped 'always', plan mode."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import permissions as P
from hexiel.config import ModelConfig, Settings
from hexiel.session import Session
from hexiel.tools import ToolDef, text_result


def test_rule_matching(tmp_path):
    cwd = str(tmp_path)
    assert P.matches("Bash(git status:*)", "bash", {"command": "git status -s"}, cwd)
    assert not P.matches("Bash(git status:*)", "bash", {"command": "git statusx"}, cwd)
    assert P.matches("Bash(npm run *)", "bash", {"command": "npm run test"}, cwd)
    assert P.matches("Read(./.env)", "read", {"path": ".env"}, cwd)
    assert P.matches("Read(.env)", "read", {"path": "sub/.env"}, cwd)          # bare name: anywhere
    assert P.matches("Edit(src/**)", "write", {"path": "src/a/b.py"}, cwd)     # Edit covers writes
    assert not P.matches("Edit(src/**)", "edit", {"path": "docs/x.md"}, cwd)
    assert P.matches("WebFetch(domain:github.com)", "web_fetch", {"url": "https://api.github.com/x"}, cwd)
    assert not P.matches("WebFetch(domain:github.com)", "web_fetch", {"url": "https://evilgithub.com"}, cwd)
    assert P.matches("mcp__gh__*", "mcp__gh__create_issue", {}, cwd)
    assert P.matches("grep", "grep", {"pattern": "x"}, cwd)


def test_precedence_and_claude_settings(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(
        {"permissions": {"allow": ["Bash(git:*)"], "deny": ["Bash(git push:*)"]}}))
    rules = P.load(str(tmp_path), {"ask": ["Bash(git commit:*)"]})
    d = lambda c: P.decide(rules, "bash", {"command": c}, str(tmp_path))
    assert d("git log") == "allow" and d("git push origin") == "deny" and d("git commit -m x") == "ask"
    assert d("ls") is None


def test_always_rule_is_scoped():
    assert P.always_rule("bash", {"command": "git status -s"}) == "Bash(git status:*)"
    assert P.always_rule("bash", {"command": "ls -la /tmp"}) == "Bash(ls:*)"
    assert P.always_rule("web_fetch", {"url": "https://docs.python.org/3/"}) == "WebFetch(domain:docs.python.org)"
    assert P.always_rule("edit", {"path": "x"}) == "Edit"


def _session(tmp_path, approvals):
    ran = []
    tools = [ToolDef("bash", "shell", {"type": "object"}, run=lambda a, c: ran.append(a["command"]) or text_result("ok")),
             ToolDef("read", "read", {"type": "object"}, readonly=True, run=lambda a, c: ran.append("read") or text_result("x"))]
    s = Session(ModelConfig(name="t", provider="openai", model="m", base_url="http://x"),
                Settings(), str(tmp_path), tools, "sys")
    answers = iter(approvals)
    s.on_approve = lambda name, args: next(answers)
    from hexiel.tooling import Toolchain
    from hexiel.tools import ToolContext
    s.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    return s, ran


def test_deny_blocks_even_readonly_and_always_is_scoped(tmp_path):
    s, ran = _session(tmp_path, ["always"])
    s.rules.deny.append("Read(.env)")
    s._execute_tool({"id": "1", "name": "read", "arguments": {"path": ".env"}})
    assert ran == [] and "Blocked by a permission rule" in s.messages[-1]["content"]
    s._execute_tool({"id": "2", "name": "bash", "arguments": {"command": "git status"}})   # asks → always
    s._execute_tool({"id": "3", "name": "bash", "arguments": {"command": "git status -s"}})  # no ask
    assert ran == ["git status", "git status -s"] and not s.auto_approve
    assert "Bash(git status:*)" in s.rules.allow


def test_plan_mode_blocks_changes(tmp_path):
    s, ran = _session(tmp_path, [])
    s.plan_mode = True
    s._execute_tool({"id": "1", "name": "bash", "arguments": {"command": "rm -rf build"}})
    s._execute_tool({"id": "2", "name": "read", "arguments": {"path": "a.py"}})
    assert ran == ["read"] and "Plan mode is on" in s.messages[0]["content"]
