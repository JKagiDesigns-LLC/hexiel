"""The working model can ask the strong model for advice, and hexiel asks for it when the model is visibly stuck."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hexiel.config as config
from hexiel import consult, planner
from hexiel.config import ModelConfig, Settings
from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, ToolDef, text_result

LOCAL = ModelConfig(name="local", provider="openai", model="small", base_url="http://x")
CLOUD = ModelConfig(name="cloud", provider="openai", model="big", base_url="http://y")


def call(name, **args):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{len(args)}{name}", "name": name, "arguments": args}]}


def build(tmp_path, monkeypatch, replies, mode="auto", planner_profile="cloud", advice="ADVICE: set the flag first"):
    ran = []

    def fail(args, ctx):
        ran.append("fail")
        return text_result("error: boom", True)

    def ok(args, ctx):
        ran.append("ok")
        return text_result("fine")
    tools = [ToolDef("fail", "always fails", {"type": "object", "properties": {}}, run=fail),
             ToolDef("ok", "works", {"type": "object", "properties": {}}, run=ok)]
    s = Session(LOCAL, Settings(auto_approve=True, planner_profile=planner_profile, consult_mode=mode),
                str(tmp_path), tools, "sys")
    s.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    s.save = lambda: None
    if mode != "off" and planner_profile:
        s.tools.append(consult.tool_def(s))
    events, shown, asked, seen = [], [], [], []
    s.on_event = lambda e, *p, **k: (events.append((e, *p)), shown.append(s.active_model) if e == "model_active" else None)
    queue = list(replies)

    def fake_once(self, provider, defs):
        seen.append([dict(m) for m in self.messages])
        return queue.pop(0)
    monkeypatch.setattr(Session, "_stream_once", fake_once)
    monkeypatch.setattr("hexiel.llm.make_provider", lambda *a, **k: object())
    monkeypatch.setattr(planner, "_config_profile", lambda session, name: {"cloud": CLOUD, "local": LOCAL}.get(name))

    def fake_complete(session, mc, system, user, max_tokens=4096):
        asked.append({"mc": mc.name, "system": system, "user": user})
        if isinstance(advice, Exception):
            raise advice
        return advice, {"prompt_tokens": 400, "completion_tokens": 60}
    monkeypatch.setattr(planner, "complete", fake_complete)
    return s, ran, events, shown, asked, seen


DONE = {"role": "assistant", "content": "done"}


def advice_messages(messages):
    return [m for m in messages if m["role"] == "user" and "Advice from cloud" in m["content"]]


def test_three_failing_calls_in_a_row_get_advice_before_the_next_step(tmp_path, monkeypatch):
    s, ran, events, shown, asked, seen = build(tmp_path, monkeypatch,
        [call("fail", n=1), call("fail", n=2), call("fail", n=3), DONE])
    s.run_turn("make the build pass")
    assert ran == ["fail"] * 3 and len(asked) == 1
    assert len(advice_messages(seen[3])) == 1 and "ADVICE: set the flag first" in advice_messages(seen[3])[0]["content"]
    assert not advice_messages(seen[2])                                    # 2 failures: not yet
    assert "make the build pass" in asked[0]["user"] and "(FAILED)" in asked[0]["user"] and "boom" in asked[0]["user"]
    assert asked[0]["mc"] == "cloud" and asked[0]["system"] == consult.CONSULT_SYSTEM
    assert any(e[0] == "note" and "last 3 tool calls in a row failed" in e[1] for e in events)
    assert any(e[0] == "note" and "advice from cloud (460 tokens)" in e[1] for e in events)
    assert "big · advising" in shown and s.active_model == ""


def test_the_same_call_repeated_three_times_counts_as_stuck(tmp_path, monkeypatch):
    s, ran, events, _, asked, seen = build(tmp_path, monkeypatch, [call("ok", q=1), call("ok", q=1), call("ok", q=1), DONE])
    s.run_turn("go")
    assert len(asked) == 1 and "repeated 3 times" in asked[0]["user"]


def test_progress_is_not_mistaken_for_being_stuck(tmp_path, monkeypatch):
    s, ran, _, _, asked, _ = build(tmp_path, monkeypatch, [call("fail", n=1), call("fail", n=2), call("ok", q=1),
                                                          call("fail", n=3), call("fail", n=4), DONE])
    s.run_turn("go")
    assert asked == []                                                      # a success resets the count of failures


def test_consult_mode_tool_never_asks_by_itself_but_the_model_can(tmp_path, monkeypatch):
    s, ran, events, _, asked, seen = build(tmp_path, monkeypatch,
        [call("fail", n=1), call("fail", n=2), call("fail", n=3), call("consult", question="why does it fail?"), DONE],
        mode="tool")
    s.run_turn("fix it")
    assert len(asked) == 1 and "why does it fail?" in asked[0]["user"]
    tool_results = [m["content"] for m in seen[-1] if m["role"] == "tool"]
    assert any(r.startswith("[advice from cloud]") and "ADVICE" in r for r in tool_results)


def test_off_disables_both(tmp_path, monkeypatch):
    s, ran, events, _, asked, seen = build(tmp_path, monkeypatch,
        [call("fail", n=1), call("fail", n=2), call("fail", n=3), DONE], mode="off")
    s.run_turn("go")
    assert asked == [] and not any(t.name == "consult" for t in s.tools)
    assert consult.unavailable_reason(s) == "consulting is turned off (consult_mode)"


def test_at_most_three_consultations_per_request(tmp_path, monkeypatch):
    replies = [call("consult", question=f"q{i}") for i in range(5)] + [DONE]
    s, ran, events, _, asked, seen = build(tmp_path, monkeypatch, replies, mode="tool")
    s.run_turn("go")
    assert len(asked) == consult.MAX_PER_TURN == 3
    results = [m["content"] for m in seen[-1] if m["role"] == "tool"]
    assert sum("consult limit reached" in r for r in results) == 2
    assert consult.used_this_turn(s) == 3
    consult.reset_turn(s)                                                   # what a new request does: the allowance renews
    assert consult.used_this_turn(s) == 0


def test_no_consultant_when_the_planner_is_the_current_model(tmp_path, monkeypatch):
    s, ran, events, _, asked, seen = build(tmp_path, monkeypatch, [call("consult", question="help"), DONE],
                                           mode="tool", planner_profile="local")
    s.run_turn("go")
    assert asked == []
    assert any("no stronger model is configured" in m["content"] for m in seen[-1] if m["role"] == "tool")


def test_a_failed_consultation_never_breaks_the_turn(tmp_path, monkeypatch):
    import httpx
    s, ran, events, _, asked, seen = build(tmp_path, monkeypatch,
        [call("fail", n=1), call("fail", n=2), call("fail", n=3), call("consult", question="help"), DONE],
        advice=httpx.ConnectError("down"))
    s.run_turn("go")
    assert s.messages[-1]["content"] == "done"
    assert any(e[0] == "note" and "could not get advice" in e[1] for e in events)
    assert any("consult failed" in m["content"] for m in seen[-1] if m["role"] == "tool")


def test_delegated_workers_consult_about_their_own_steps_not_the_parents(tmp_path, monkeypatch):
    s, *_ = build(tmp_path, monkeypatch, [DONE])
    helper = planner.make_helper(s, LOCAL, "sys", list(s.tools))
    mine = next(t for t in helper.tools if t.name == "consult")
    parents = next(t for t in s.tools if t.name == "consult")
    assert mine is not parents
    seen = []
    monkeypatch.setattr(consult, "ask", lambda session, question, reason="": seen.append(session) or "advice")
    helper.messages.append({"role": "user", "content": "my own step"})
    mine.run({"question": "q"}, None)
    assert seen == [helper]


def test_the_setting_is_editable_and_validated(tmp_path, monkeypatch):
    from hexiel import settings_edit
    s, *_ = build(tmp_path, monkeypatch, [DONE])
    assert any(r["key"] == "settings.consult_mode" and r["label"] == "Ask for advice" for r in settings_edit.rows(s))
    cfg = tmp_path / "config.toml"
    cfg.write_text('[models.x]\nprovider = "openai"\nmodel = "m"\nbase_url = "http://h/v1"\n[settings]\nconsult_mode = "sometimes"\n')
    monkeypatch.setattr(config, "CONFIG_FILE", cfg)
    assert config.load_config().settings.consult_mode == "auto"


def test_the_tool_is_registered_only_when_a_strong_model_is_configured(tmp_path, monkeypatch):
    from hexiel.main import build_session
    cfg = tmp_path / "config.toml"
    base = ('default = "x"\n[models.x]\nprovider = "openai"\nmodel = "m"\nbase_url = "http://h/v1"\n'
            '[models.y]\nprovider = "openai"\nmodel = "n"\nbase_url = "http://h/v1"\n[settings]\n')
    monkeypatch.setattr(config, "CONFIG_FILE", cfg)
    cfg.write_text(base + 'planner_profile = "y"\n')
    assert any(t.name == "consult" for t in build_session(str(tmp_path), None).tools)
    cfg.write_text(base)
    assert not any(t.name == "consult" for t in build_session(str(tmp_path), None).tools)
    cfg.write_text(base + 'planner_profile = "y"\nconsult_mode = "off"\n')
    assert not any(t.name == "consult" for t in build_session(str(tmp_path), None).tools)
