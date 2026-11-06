"""0.8.5 speed work: stable prompt prefix, Ollama resident models, /context metering."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import llm, prompt
from hexiel.config import ModelConfig, Settings
from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, build_tools


def make(cwd, **kw):
    MC = ModelConfig(name="t", provider="openai", model="m", base_url=kw.pop("base_url", ""),
                     context_window=kw.pop("context_window", 200_000), **kw)
    s = Session(MC, Settings(), cwd=str(cwd), tools=build_tools(Toolchain()), system_prompt="sys")
    s.bind_tool_ctx(ToolContext(cwd=str(cwd), chain=Toolchain()))
    return s


# ---------------------------------------------------------------- stable prefix / env note

def test_system_prompt_does_not_embed_volatile_facts(tmp_path):
    sys1 = prompt.build_system_prompt(str(tmp_path), "", "")
    sys2 = prompt.build_system_prompt(str(tmp_path), "", "")
    assert "Today's date" not in sys1
    assert "branch:" not in sys1 and "HEAD:" not in sys1   # no live git state in the system prompt
    assert sys1 == sys2


def test_env_note_carries_the_facts(tmp_path):
    note = prompt.env_note(str(tmp_path))
    assert "Today's date" in note
    assert "branch:" in note or "git:" in note


def test_user_turns_carry_env_note(tmp_path, monkeypatch):
    """A real user turn opens with the fresh environment facts (stable prefix stays cached)."""
    s = make(tmp_path, base_url="http://127.0.0.1:9/v1")

    class Fake:
        def stream(self, messages, system, tools, **_kw):
            s._sent = messages[-1]
            yield ("text", "ok")
            yield ("assistant", {"role": "assistant", "content": "ok"})
            yield ("done", {"prompt_tokens": 10, "completion_tokens": 2})
    monkeypatch.setattr(llm, "make_provider", lambda mc, settings: Fake())
    s.on_event = lambda e, *p, **k: None
    s.run_turn("plain question about my machine")
    sent = s._sent["content"]
    assert sent.startswith("[environment — fresh facts") and "Today's date" in sent
    assert "plain question about my machine" in sent
    s._stop.set()


# ---------------------------------------------------------------- Ollama resident models

def test_local_detection():
    p = llm.OpenAICompatProvider("", "http://localhost:11434/v1")
    assert p._local()
    p2 = llm.OpenAICompatProvider("", "http://192.168.1.20:11434")
    assert p2._local()
    p3 = llm.OpenAICompatProvider("", "https://api.openai.com/v1")
    assert not p3._local()
    p4 = llm.OpenAICompatProvider("", "https://ollama.com/v1")
    assert not p4._local()


def test_payload_local_fields():
    """keep_alive/num_ctx ride only on local endpoints, never on cloud ones."""
    p = llm.OpenAICompatProvider("", "http://localhost:11434/v1")
    body = p._payload([{"role": "user", "content": "hi"}], "sys", [], model="qwen:3b",
                      max_output_tokens=8, max_tokens_param="max_tokens", extra_body={},
                      keep_alive="1h", num_ctx=8192)
    assert body["keep_alive"] == "1h"
    assert body["options"]["num_ctx"] == 8192
    # user extra_body options win
    body = p._payload([{"role": "user", "content": "hi"}], "sys", [], model="m",
                      max_output_tokens=8, max_tokens_param="max_tokens",
                      extra_body={"options": {"num_ctx": 4096}},
                      keep_alive="", num_ctx=8192)
    assert body["options"]["num_ctx"] == 4096 and "keep_alive" not in body

    # cloud endpoints get none of that; OpenAI gets the stable cache key
    pc = llm.OpenAICompatProvider("", "https://api.openai.com/v1")
    body = pc._payload([{"role": "user", "content": "hi"}], "sys", [], model="gpt-5",
                       max_output_tokens=8, max_tokens_param="max_completion_tokens",
                       extra_body={}, keep_alive="1h", num_ctx=8192,
                       conversation_id="session-42")
    assert "keep_alive" not in body and "options" not in body
    assert body["prompt_cache_key"] == "session-42"


def test_model_config_defaults_keep_fields(tmp_path):
    from hexiel.config import ModelConfig
    mc = ModelConfig(name="t", provider="openai", model="m", base_url="http://x")
    assert mc.keep_alive == "1h" and mc.num_ctx == 0


# ---------------------------------------------------------------- /context metering

def test_context_breakdown_and_clear(tmp_path):
    s = make(tmp_path)
    s.system_prompt = "S" * 400
    s.messages = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "a1", "name": "grep", "arguments": {"q": "x"}}]},
        {"role": "tool", "tool_call_id": "a1", "content": "L" * 4000},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "a2", "name": "bash", "arguments": {"cmd": "ls"}}]},
        {"role": "tool", "tool_call_id": "a2", "content": "M" * 4000},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "q3"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "a3", "name": "bash", "arguments": {"cmd": "ls"}}]},
        {"role": "tool", "tool_call_id": "a3", "content": "K" * 4000},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "q4"},
    ]
    b = s.context_breakdown()
    assert b["system"] > 0 and b["messages"]["tool"] > 1000
    before = len("".join(m["content"] for m in s.messages
                         if m["role"] == "tool" and isinstance(m.get("content"), str)))
    cleared = s.clear_old_tool_results(keep=2)
    assert cleared == 1, "the first round's tool result should go; the last two stay"
    after = len("".join(m.get("content", "") for m in s.messages
                        if m["role"] == "tool" and isinstance(m.get("content"), str)))
    assert after < before
    assert s.messages[2]["content"].startswith("[cleared: grep]")
    assert s.messages[6]["content"] not in ("", None)
    assert not s.messages[6]["content"].startswith("[cleared")


def test_clear_keeps_recent_and_is_noop(tmp_path):
    s = make(tmp_path)
    s.messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "a", "name": "bash", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "a", "content": "out"},
    ]
    assert s.clear_old_tool_results(keep=3) == 0
    assert s.messages[2]["content"] == "out"
