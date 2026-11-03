"""Sidebar 'where am I running' chips, and the retry when the model's own tool call is unparseable."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import llm
from hexiel import session as session_mod
from hexiel.config import ModelConfig, Settings
from hexiel.session import Session
from hexiel.tui import where_rows
from hexiel.where import detect

NO = lambda p: False  # noqa: E731


def test_plain_terminal_has_no_chips():
    assert detect(env={}, read=lambda p: "", hostname=lambda: "box", exists=NO) == []


def test_ssh_session_shows_the_machine():
    got = detect(env={"SSH_CONNECTION": "1.2.3.4 5 6.7.8.9 22"}, read=lambda p: "", hostname=lambda: "build01", exists=NO)
    assert got == [("ssh", "build01")]


def test_docker_container_over_ssh():
    got = detect(env={"SSH_TTY": "/dev/pts/1"}, read=lambda p: "", hostname=lambda: "abc123",
                 exists=lambda p: p == "/.dockerenv")
    assert got == [("ssh", "abc123"), ("docker", "abc123")]


def test_podman_name_comes_from_containerenv():
    got = detect(env={}, read=lambda p: 'name="sci-dev"\nid="x"\n' if p == "/run/.containerenv" else "",
                 hostname=lambda: "h", exists=lambda p: p == "/run/.containerenv")
    assert got == [("podman", "sci-dev")]


def test_chip_rows_render():
    rows = where_rows(27)
    assert isinstance(rows, list)


def test_malformed_call_text_is_recognised():
    assert llm.is_malformed_tool_call('provider 500: {"error":{"message":"XML syntax error on line 10: element <parameter> closed by </function>"}}')
    assert not llm.is_malformed_tool_call("provider 500: out of memory")


def test_malformed_tool_call_is_retried_with_a_nudge(tmp_path, monkeypatch):
    mc = ModelConfig(name="t", provider="openai", model="m", base_url="http://x/v1")
    s = Session(mc, Settings(), str(tmp_path), [], "sys")
    calls = []

    def fake(provider, tool_defs):
        calls.append(list(s.messages))
        if len(calls) < 3:
            s._provider_error = "provider 500: XML syntax error on line 10: element <parameter> closed by </function>"
            return {"role": "assistant", "content": ""}
        return {"role": "assistant", "content": "done"}

    monkeypatch.setattr(s, "_stream_assistant", fake)
    events = []
    s.on_event = lambda kind, *a, **k: events.append((kind, a))
    s.run_turn("go")
    assert len(calls) == 3
    assert session_mod.MALFORMED_CALL_NUDGE in [m.get("content") for m in calls[2]]
    assert ("turn_end", ()) in events
