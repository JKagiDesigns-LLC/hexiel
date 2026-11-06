"""/prompt: view and edit the system prompt — this session, this directory, or globally.

Precedence is session > directory > global > the default build; the session copy
rides the saved chat, the directory/global copies are markdown files the user
(or the repo) can also edit by hand."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from hexiel import prompt_edit
from hexiel.config import ModelConfig, Settings
from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, build_tools

MC = ModelConfig(name="t", provider="openai", model="m", base_url="http://x")

DEFAULT = "guardian-angel default prompt"


def session(cwd) -> Session:
    tools = [t for t in build_tools(Toolchain()) if t.name == "bash"]
    s = Session(MC, Settings(), str(cwd), tools, DEFAULT)
    s.base_prompt = DEFAULT
    return s


def saved_payload(s: Session) -> dict:
    s.save()
    from hexiel.config import home_data_dir
    path = next((home_data_dir() / "sessions").glob("*.json"))
    return json.loads(path.read_text())


def test_session_scope_keeps_the_edit_with_the_saved_chat(tmp_path):
    s = session(tmp_path)
    prompt_edit.save(s, "MY-PROMPT", "session")
    assert s.system_prompt == "MY-PROMPT" and prompt_edit.source_of(s) == "session"
    payload = saved_payload(s)
    assert payload["prompt_override"] == "session" and payload["system_prompt"] == "MY-PROMPT"

    resumed = session(tmp_path)
    prompt_edit.startup(resumed, payload)
    assert resumed.system_prompt == "MY-PROMPT" and resumed.prompt_override == "session"
    # a secret typed into the prompt before redaction existed is cleaned once on load
    s2 = session(tmp_path)
    payload2 = saved_payload(s2)
    prompt_edit.save(s2, "GITHUB_TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUV", "session")
    payload2["system_prompt"] = "GITHUB_TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUV"
    resumed2 = session(tmp_path)
    prompt_edit.startup(resumed2, payload2)
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUV" not in resumed2.system_prompt


def test_directory_scope_writes_repo_file_and_startup_applies_it(tmp_path):
    s = session(tmp_path)
    prompt_edit.save(s, "DIR-PROMPT", "directory")
    path = prompt_edit.dir_path(str(tmp_path))
    assert path.read_text().strip() == "DIR-PROMPT"

    fresh = session(tmp_path)
    assert prompt_edit.source_of(fresh) == "directory"
    prompt_edit.startup(fresh, None)
    assert fresh.system_prompt == "DIR-PROMPT" and fresh.prompt_override == "directory"


def test_global_scope_uses_home_config(tmp_path):
    s = session(tmp_path)
    prompt_edit.save(s, "GLOBAL-PROMPT", "global")
    assert prompt_edit.global_path().read_text().strip() == "GLOBAL-PROMPT"

    # the directory file beats the global one
    (tmp_path / ".hexiel").mkdir(exist_ok=True)
    prompt_edit.dir_path(str(tmp_path)).write_text("DIR-PROMPT\n")
    fresh = session(tmp_path)
    prompt_edit.startup(fresh, None)
    assert fresh.system_prompt == "DIR-PROMPT"

    (tmp_path / ".hexiel" / "system-prompt.md").unlink()
    fresh2 = session(tmp_path)
    prompt_edit.startup(fresh2, None)
    assert fresh2.system_prompt == "GLOBAL-PROMPT" and fresh2.prompt_override == "global"


def test_reset_puts_everything_back(tmp_path):
    s = session(tmp_path)
    prompt_edit.save(s, "DIR-PROMPT", "directory")
    prompt_edit.save(s, "SESSION-PROMPT", "session")
    prompt_edit.save(s, "GLOBAL-PROMPT", "global")
    msg = prompt_edit.reset(s)
    assert s.system_prompt == DEFAULT and s.system_prompt != "GLOBAL-PROMPT"
    assert prompt_edit.source_of(s) is None
    assert not prompt_edit.dir_path(str(tmp_path)).exists()
    assert not prompt_edit.global_path().exists()
    assert "default prompt" in msg
    # nothing lingering in the saved chat
    payload = saved_payload(s)
    assert "system_prompt" not in payload


def test_empty_edit_is_rejected_not_saved(tmp_path):
    s = session(tmp_path)
    with pytest.raises(ValueError):
        prompt_edit.save(s, "   \n  ", "session")
    assert s.system_prompt == DEFAULT and prompt_edit.source_of(s) is None


def test_get_reports_source_and_default(tmp_path):
    s = session(tmp_path)
    info = prompt_edit.get(s)
    assert info["source"] == "default" and info["default"] == DEFAULT
    prompt_edit.save(s, "EDITED", "session")
    info = prompt_edit.get(s)
    assert info["source"] == "session" and info["text"] == "EDITED"
