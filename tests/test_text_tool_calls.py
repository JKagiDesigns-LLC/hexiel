"""A model writes its tool call into the reply TEXT instead of the tool-call channel (gpt-oss on
Ollama leaks its built-in `{"cmd": [...]}` shell format this way): hexiel turns the text back into
a real call, so the turn keeps working instead of ending on a raw JSON blob."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel.session import _match_text_call, _text_tool_call
from hexiel.tools import Toolchain, build_tools
from tests.test_remote_control import make_session

TOOLS = build_tools(Toolchain())


def test_gpt_oss_builtin_shell_format_becomes_a_bash_call():
    assert _text_tool_call('{"cmd":["bash","-lc","ls -R"]}', TOOLS) == ("bash", {"command": "ls -R"})
    assert _text_tool_call('{"cmd":["git","status"]}', TOOLS) == ("bash", {"command": "git status"})


def test_a_string_command_matches_bash():
    assert _text_tool_call('{"command":"ls -la"}', TOOLS) == ("bash", {"command": "ls -la"})


def test_wrapper_forms():
    assert _text_tool_call('{"name":"bash","arguments":{"command":"pwd"}}', TOOLS) == \
        ("bash", {"command": "pwd"})
    assert _text_tool_call('{"tool":"read","args":{"path":"a.py"}}', TOOLS) == ("read", {"path": "a.py"})
    assert _text_tool_call('{"type":"function","function":{"name":"read","arguments":"{\\"path\\": \\"a.py\\"}"}}',
                           TOOLS) == ("read", {"path": "a.py"})


def test_plain_keys_that_fit_one_tool():
    assert _text_tool_call('{"path":"notes.md","offset":5,"limit":10}', TOOLS) == \
        ("read", {"path": "notes.md", "offset": 5, "limit": 10})


def test_a_json_answer_that_fits_no_tool_is_left_alone():
    assert _text_tool_call('{"route":"chat","reason":"greeting"}', TOOLS) is None
    assert _text_tool_call('{"tokens":{"a":1,"b":2}}', TOOLS) is None


def test_required_keys_missing_is_left_alone():
    assert _text_tool_call('{"path":"a.py"}', TOOLS) is None or \
        _text_tool_call('{"path":"a.py"}', TOOLS)[0] == "read"   # read needs no required keys


def test_a_data_dump_inside_long_prose_is_not_a_call():
    prose = "Here is the data you asked for. " + ("word " * 600)
    assert _text_tool_call(prose + '{"command":"ls"}', TOOLS) is None


def test_a_call_in_short_prose_is_rescued():
    hit = _text_tool_call('Let me check. {"cmd":["bash","-lc","ls -R"]}', TOOLS)
    assert hit == ("bash", {"command": "ls -R"})


def test_examples_in_an_answer_are_shown_not_run():
    fenced = 'Add this to your config:\n```json\n{"command": "npm test"}\n```'
    assert _text_tool_call(fenced, TOOLS) is None
    mid = '{"command": "rm -rf build"} is what the cleanup step runs, so check it before merging.'
    assert _text_tool_call(mid, TOOLS) is None


def test_garbage_text_is_left_alone():
    assert _text_tool_call("no json here at all", TOOLS) is None
    assert _text_tool_call('{"cmd": ["unterminated', TOOLS) is None


def _turn(tmp_path, monkeypatch, outcomes):
    """A session whose model answers with the given messages; tools really run."""
    from hexiel.config import ModelConfig, Settings
    from hexiel.session import Session
    from hexiel.tools import ToolContext
    s = Session(ModelConfig(name="m", provider="openai", model="m", base_url="http://127.0.0.1:9/v1"),
                Settings(auto_approve=True), str(tmp_path), TOOLS, "sys")
    s.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    s.save = lambda: None
    events = []
    s.on_event = lambda e, *p, **k: events.append((e, *p))
    seq = iter(outcomes)
    monkeypatch.setattr(Session, "_stream_once", lambda self, provider, tool_defs: next(seq))
    monkeypatch.setattr("hexiel.llm.make_provider", lambda *a, **k: object())
    return s, events


def test_a_text_leaked_call_runs_and_the_turn_continues(tmp_path, monkeypatch):
    s, events = _turn(tmp_path, monkeypatch, [
        {"role": "assistant", "content": '{"cmd":["bash","-lc","ls -R"]}'},
        {"role": "assistant", "content": "all done"}])
    s.run_turn("look around")
    calls = [m for m in s.messages if m.get("role") == "assistant" and m.get("tool_calls")]
    assert calls and calls[0]["tool_calls"][0]["name"] == "bash"
    assert calls[0]["tool_calls"][0]["arguments"] == {"command": "ls -R"}
    results = [m for m in s.messages if m.get("role") == "tool"]
    assert results and "is_error" not in results[0]
    assert any("running it anyway" in str(e[1]) for e in events if e[0] == "note")
    assert s.messages[-1]["content"] == "all done"


def test_a_leaked_call_that_fits_no_tool_ends_the_turn_normally(tmp_path, monkeypatch):
    s, events = _turn(tmp_path, monkeypatch, [
        {"role": "assistant", "content": '{"route":"chat","reason":"greeting"}'}])
    s.run_turn("hi")
    assert s.messages[-1]["content"] == '{"route":"chat","reason":"greeting"}'
    assert not [m for m in s.messages if m.get("role") == "tool"]
