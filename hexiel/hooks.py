"""Hooks: run your own commands on agent events (Claude Code compatible).

    {"hooks": {"PostToolUse": [{"matcher": "Edit|Write",
                                "hooks": [{"type": "command", "command": "ruff format \\"$FILE\\""}]}]}}

Read from <project>/.claude/settings.json, .claude/settings.local.json,
.hexiel/settings.json and ~/.config/hexiel/settings.json. Events: PreToolUse,
PostToolUse, UserPromptSubmit, Stop, SessionStart. Each hook gets JSON on stdin
(session_id, cwd, hook_event_name, tool_name, tool_input, tool_response,
prompt). Exit code 2 = block: PreToolUse stops the tool call, UserPromptSubmit
stops the prompt, PostToolUse feeds stderr back to the model. Other non-zero
codes are reported but don't block. UserPromptSubmit / SessionStart stdout is
added to the conversation as context."""

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .permissions import RULE_NAME

EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop", "SessionStart", "WorktreePostSetup")


@dataclass
class Hook:
    event: str
    matcher: str
    command: str
    timeout: float = 60.0


@dataclass
class Outcome:
    code: int
    stdout: str
    stderr: str
    command: str

    @property
    def blocked(self) -> bool:
        return self.code == 2


def load(cwd: str) -> list[Hook]:
    from .config import home_config_dir
    hooks: list[Hook] = []
    for path in (home_config_dir() / "settings.json", Path(cwd) / ".claude" / "settings.json",
                 Path(cwd) / ".claude" / "settings.local.json", Path(cwd) / ".hexiel" / "settings.json"):
        try:
            data = json.loads(path.read_text()).get("hooks") or {}
        except (OSError, ValueError, AttributeError):
            continue
        for event, groups in data.items():
            if event not in EVENTS:
                continue
            for group in groups or []:
                for h in group.get("hooks") or []:
                    if h.get("type", "command") == "command" and h.get("command"):
                        hooks.append(Hook(event, str(group.get("matcher", "")), h["command"],
                                          float(h.get("timeout", 60))))
    return hooks


def _matches(matcher: str, tool: str) -> bool:
    if matcher in ("", "*") or not tool:
        return True
    names = {tool, RULE_NAME.get(tool, tool)}
    try:
        return any(re.fullmatch(matcher, n) for n in names)
    except re.error:
        return matcher in names


def _claude_input(tool: str, args: dict) -> dict:
    """Claude Code names file paths file_path; mirror that so hooks port over."""
    out = dict(args)
    if "path" in out and tool in ("read", "write", "edit", "view_image", "image_edit"):
        out.setdefault("file_path", out["path"])
    return out


def run(hooks: list[Hook], event: str, cwd: str, session_id: str, *, tool: str = "",
        tool_input: dict | None = None, tool_response: str | None = None,
        prompt: str | None = None, worktree: str | None = None) -> list[Outcome]:
    selected = [h for h in hooks if h.event == event and (event not in ("PreToolUse", "PostToolUse")
                                                          or _matches(h.matcher, tool))]
    if not selected:
        return []
    payload = {"session_id": session_id, "cwd": cwd, "hook_event_name": event}
    if worktree is not None:
        payload["worktree"] = worktree                 # WorktreePostSetup: the fresh worktree
    if tool:
        payload["tool_name"] = RULE_NAME.get(tool, tool)
        payload["tool_input"] = _claude_input(tool, tool_input or {})
    if tool_response is not None:
        payload["tool_response"] = tool_response[:20000]
    if prompt is not None:
        payload["prompt"] = prompt
    data = json.dumps(payload)
    env_file = str((tool_input or {}).get("path", ""))
    outcomes = []
    for h in selected:
        try:
            proc = subprocess.run(h.command, shell=True, input=data, capture_output=True, text=True,  # nosec B602
                                  cwd=cwd, timeout=h.timeout,
                                  env={**__import__("os").environ, "HEXIEL_PROJECT_DIR": cwd,
                                       "CLAUDE_PROJECT_DIR": cwd, "FILE": env_file})
            outcomes.append(Outcome(proc.returncode, proc.stdout.strip(), proc.stderr.strip(), h.command))
        except subprocess.TimeoutExpired:
            outcomes.append(Outcome(1, "", f"hook timed out after {h.timeout:.0f}s", h.command))
        except OSError as exc:
            outcomes.append(Outcome(1, "", str(exc), h.command))
    return outcomes
