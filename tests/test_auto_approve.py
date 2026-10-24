"""auto_approve = false | "except-commits" | true: everything runs without asking, except (in the middle mode)
anything that commits or publishes history to git or svn."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hexiel.config as config
from hexiel import settings_edit
from hexiel.config import ModelConfig, Settings, approve_mode, parse_auto_approve
from hexiel.permissions import commit_action
from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, ToolDef, text_result

MC = ModelConfig(name="t", provider="openai", model="m", base_url="http://x")

COMMITS = [
    'git commit -m "x"', 'git add -A && git commit -am wip', 'git -C /repo commit -m x', 'git push origin main', 'git push',
    'cd x; git merge feature', 'git rebase -i HEAD~3', 'git cherry-pick abc', 'git revert HEAD', 'git ci -m hi',
    'git-commit -m x', 'svn commit -m "x"', 'svn ci -m x', 'svn import . http://h/r',
    'svn copy trunk https://h/r/tags/1 -m rel', 'svn delete https://h/r/old -m x', 'svnmucc -m x mkdir http://h/r/new',
    'bash -c "cd repo && git commit -m x"', 'sudo git push', 'echo hi && GIT_AUTHOR_NAME=a git commit -m x',
    'git --no-pager commit', 'git commit', 'git am < patch',
    'python3 -c "import subprocess; subprocess.run([\'git\',\'-C\',d,\'commit\',\'-m\',m])"',
]
HARMLESS = [
    'git status', 'git diff HEAD~1', 'git log --oneline -5', 'git add -A', 'git checkout -b feature', 'git stash',
    'git fetch origin', 'git pull', 'git branch -a', 'git log --grep=fix', 'svn status', 'svn update', 'svn diff',
    'svn log -l 5', 'svn delete old.txt', 'svn copy a.txt b.txt', 'ls -la', 'npm test', 'pytest -q',
    'echo "committed" > notes.txt', 'grep -rn "commit" src/', 'docker compose up -d', 'rm -rf build/',
]


@pytest.mark.parametrize("command", COMMITS)
def test_commits_and_pushes_are_recognised(command):
    assert commit_action("bash", {"command": command}), command


@pytest.mark.parametrize("command", HARMLESS)
def test_ordinary_commands_are_not_flagged(command):
    assert commit_action("bash", {"command": command}) == "", command


def test_other_tools_are_scanned_only_when_they_run_commands():
    assert commit_action("python", {"code": "import os; os.system('git commit -m x')"}) == "git commit"
    assert commit_action("container", {"action": "exec", "container": "c", "cmd": "git push"}) == "git push"
    assert commit_action("mcp__github__push_files", {}) == "mcp__github__push_files"
    assert commit_action("mcp__shell__run", {"command": "git commit -m x"}) == "git commit"
    assert commit_action("write", {"path": "HOWTO.md", "content": "run git commit when done"}) == ""
    assert commit_action("mcp__github__list_commits", {}, readonly=True) == ""


# ---------------------------------------------------------------- the session

def session(tmp_path, monkeypatch, command, mode, verdicts=("yes",), rules=None):
    ran, asked = [], []

    def run_bash(args, ctx):
        ran.append(args["command"])
        return text_result("done")
    tools = [ToolDef("bash", "shell", {"type": "object", "properties": {}}, run=run_bash)]
    s = Session(MC, Settings(auto_approve=mode != "off", guard_commits=mode == "except-commits"),
                str(tmp_path), tools, "sys")
    s.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    s.save = lambda: None
    if rules:
        s.rules.extend(rules)
    answers = iter(verdicts)
    s.on_approve = lambda tool, args: (asked.append(args) or next(answers))
    events = []
    s.on_event = lambda e, *p, **k: events.append((e, *p))
    replies = iter([{"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "name": "bash", "arguments": {"command": command}}]}, {"role": "assistant", "content": "ok"}])
    monkeypatch.setattr(Session, "_stream_once", lambda self, provider, defs: next(replies))
    monkeypatch.setattr("hexiel.llm.make_provider", lambda *a, **k: object())
    s.run_turn("go")
    return s, ran, asked, events


def test_off_asks_for_everything(tmp_path, monkeypatch):
    _, ran, asked, _ = session(tmp_path, monkeypatch, "ls", "off")
    assert asked and ran == ["ls"]


def test_except_commits_runs_ordinary_commands_without_asking(tmp_path, monkeypatch):
    _, ran, asked, _ = session(tmp_path, monkeypatch, "rm -rf build && npm test", "except-commits")
    assert asked == [] and ran == ["rm -rf build && npm test"]


def test_except_commits_asks_before_a_commit_and_respects_no(tmp_path, monkeypatch):
    s, ran, asked, events = session(tmp_path, monkeypatch, 'git commit -am "wip"', "except-commits", verdicts=("no",))
    assert len(asked) == 1 and ran == []                              # asked, refused, nothing ran
    assert any(e[0] == "note" and "git commit" in e[1] and "asking first" in e[1] for e in events)
    assert any("denied" in m.get("content", "") for m in s.messages if m["role"] == "tool")


def test_except_commits_runs_a_commit_once_approved(tmp_path, monkeypatch):
    _, ran, asked, _ = session(tmp_path, monkeypatch, 'git push origin main', "except-commits", verdicts=("yes",))
    assert len(asked) == 1 and ran == ["git push origin main"]


def test_always_on_a_commit_does_not_switch_the_guard_off(tmp_path, monkeypatch):
    s, ran, asked, _ = session(tmp_path, monkeypatch, "git commit -m a", "except-commits", verdicts=("always",))
    assert ran == ["git commit -m a"]                                 # this one runs...
    assert not any("commit" in r for r in s.rules.allow)             # ...but no blanket rule was created
    assert commit_action("bash", {"command": "git commit -m b"})


def test_everything_mode_runs_commits_without_asking(tmp_path, monkeypatch):
    _, ran, asked, _ = session(tmp_path, monkeypatch, 'git commit -m x && git push', "all")
    assert asked == [] and ran == ["git commit -m x && git push"]


def test_an_explicit_allow_rule_is_honoured(tmp_path, monkeypatch):
    from hexiel.permissions import Rules
    _, ran, asked, _ = session(tmp_path, monkeypatch, "git commit -m x", "except-commits",
                               rules={"allow": ["Bash(git commit:*)"]})
    assert asked == [] and ran == ["git commit -m x"]


def test_deny_rules_still_win_in_every_mode(tmp_path, monkeypatch):
    for mode in ("except-commits", "all"):
        _, ran, asked, _ = session(tmp_path, monkeypatch, "rm secrets.txt", mode, rules={"deny": ["Bash(rm secrets.txt)"]})
        assert ran == [] and asked == []


# ---------------------------------------------------------------- configuration

def test_config_values():
    assert [parse_auto_approve(v) for v in (False, None, True, "except-commits", "Except_Commits", "all", "off", "everything")] \
        == ["off", "off", "all", "except-commits", "except-commits", "all", "off", "all"]
    with pytest.raises(ValueError, match="false, true or"):
        parse_auto_approve("sometimes")
    assert [approve_mode(a, g) for a, g in ((False, False), (True, True), (True, False))] == ["off", "except-commits", "all"]


@pytest.mark.parametrize("value, auto, guard", [("false", False, False), ('"except-commits"', True, True), ("true", True, False)])
def test_config_file_modes_load(tmp_path, monkeypatch, value, auto, guard):
    path = tmp_path / "config.toml"
    path.write_text(f'[models.x]\nprovider = "openai"\nmodel = "m"\nbase_url = "http://h/v1"\n[settings]\nauto_approve = {value}\n')
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    st = config.load_config().settings
    assert (st.auto_approve, st.guard_commits) == (auto, guard)


def test_a_bad_value_gives_a_readable_error(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[models.x]\nprovider = "openai"\nmodel = "m"\nbase_url = "http://h/v1"\n[settings]\nauto_approve = "maybe"\n')
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    with pytest.raises(SystemExit, match='auto_approve must be false, true or "except-commits"'):
        config.load_config()


def test_settings_editor_changes_the_mode_live_and_in_the_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[models.t]\nprovider = "openai"\nmodel = "m"\nbase_url = "http://h/v1"\n'
                    '[settings]\nauto_approve = false   # ask first\n')
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    s = Session(MC, Settings(), str(tmp_path), [], "sys")
    row = next(r for r in settings_edit.rows(s, with_options=True) if r["key"] == "settings.auto_approve")
    assert row["value"] == "off" and row["options"] == ["off", "except commits", "everything"]
    msg = settings_edit.apply(s, "settings.auto_approve", "except commits", "permanent")
    assert (s.auto_approve, s.guard_commits) == (True, True) and "except commits" in msg
    assert 'auto_approve = "except-commits"' in path.read_text() and "# ask first" in path.read_text()
    assert config.load_config().settings.guard_commits is True
    settings_edit.apply(s, "settings.auto_approve", "everything", "permanent")
    assert (s.auto_approve, s.guard_commits) == (True, False) and "auto_approve = true" in path.read_text()
    settings_edit.apply(s, "settings.auto_approve", False, "session")                 # the legacy boolean still works
    assert (s.auto_approve, s.guard_commits) == (False, False)


# ---------------------------------------------------------------- UIs

def test_tui_auto_command_cycles_and_the_status_line_says_which_mode(tmp_path):
    from hexiel.tui import HexielTUI

    async def go():
        app = HexielTUI(Session(MC, Settings(), str(tmp_path), [], "sys"), [], set())
        async with app.run_test(size=(150, 30)) as pilot:
            seen = []
            for arg in ("", "", "", "commits", "all", "off"):
                app.cmd_auto(arg)
                await pilot.pause(0.1)
                seen.append((approve_mode(app.session.auto_approve, app.session.guard_commits), str(app.status.content)))
            return seen
    seen = asyncio.run(go())
    assert [m for m, _ in seen] == ["except-commits", "all", "off", "except-commits", "all", "off"]
    assert "asks before commits" in seen[0][1] and "AUTO-APPROVE ALL" in seen[1][1] and "asks before writes" in seen[2][1]


def test_delegated_workers_and_plan_checks_inherit_the_guard(tmp_path):
    from hexiel import planner
    s = Session(MC, Settings(auto_approve=True, guard_commits=True, delegate_approval=True), str(tmp_path), [], "sys")
    s.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    helper = planner.make_helper(s, MC, "sys", [])
    assert helper.auto_approve and helper.guard_commits
    asked = []
    s.on_approve = lambda tool, args: (asked.append(args["command"]) or "no")
    assert planner._check_allowed(s, "pytest -q") is True and asked == []
    assert planner._check_allowed(s, "git commit -m x") is False and asked == ["git commit -m x"]
