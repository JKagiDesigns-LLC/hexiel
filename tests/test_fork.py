"""#27 /fork — turn-boundary slicing (env note / steering / compaction aware), the
fork payload snapshot, and the TUI swap that replays the branch into the chat."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hexiel import session as sess_mod
from hexiel.session import Session, fork_payload, real_turn_starts, strip_env_note
from tests.test_tui import make_app, run


def msg(role: str, content: str, **extra) -> dict:
    m = {"role": role, "content": content}
    m.update(extra)
    return m


def env_wrap(text: str) -> str:
    return sess_mod.ENV_MARK + "\nToday's date: 2026-11-06\nbranch: main\n\n" + text


def sample() -> list[dict]:
    return [
        msg("user", env_wrap("fix the widget")),                                   # turn 1
        msg("assistant", "thinking…", tool_calls=[{"id": "c1", "name": "read",
                                                   "arguments": {}}]),
        msg("tool", "file body", tool_call_id="c1"),
        msg("assistant", "fixed it"),
        msg("user", "<auto-compaction>\n\neverything earlier is summarized here"),  # synthetic
        msg("assistant", "recap of earlier work"),
        msg("user", sess_mod.STEERING_NOTE + "keep going"),                        # mid-turn steering
        msg("user", env_wrap("now the footer")),                                   # turn 2
        msg("assistant", "footer done"),
        msg("user", sess_mod.ENV_MARK + "\nToday's date: 2026-11-06\nbranch: main"),  # legacy standalone env
    ]


def fake_session(tmp_path: Path, messages: list[dict], **attrs) -> Session:
    mc = mc_of()
    s = Session(mc, Settings(), str(tmp_path), [], "sys")
    s.messages = messages
    for k, v in attrs.items():
        setattr(s, k, v)
    return s


def mc_of():
    from hexiel.config import ModelConfig
    return ModelConfig(name="t", provider="openai", model="m", base_url="")


def Settings():
    from hexiel.config import Settings
    return Settings()


# ---------------------------------------------------------------- slicing rules

def test_strip_env_note():
    assert strip_env_note(env_wrap("hello")) == "hello"
    assert strip_env_note("plain question") == "plain question"
    note_only = sess_mod.ENV_MARK + "\nToday's date: 2026-11-06"
    assert strip_env_note(note_only) == ""             # legacy standalone note: no user words


def test_real_turn_starts_skips_synthetic(tmp_path):
    msgs = sample()
    starts = real_turn_starts(msgs)
    assert starts == [0, 7]                            # turn 1 (env rides inline) and turn 2
    # a legacy standalone env note and steering/compaction never open a turn
    assert real_turn_starts(msgs[4:]) == [3]           # only turn 2's message is real from index 4


def test_fork_payload_default_branches_from_here(tmp_path):
    s = fake_session(tmp_path, sample(), id="orig12")
    data, kept = fork_payload(s)
    assert kept == 2
    # everything through the end of turn 2; the trailing legacy env note is dropped
    assert data["messages"] == sample()[:9]
    assert data["id"] != "orig12" and len(data["id"]) == 12
    assert data["cwd"] == str(tmp_path) and data["model"] == "t"
    assert data["messages"][0]["content"] == sample()[0]["content"]   # byte-faithful


def test_fork_payload_upto_keeps_turns_1_to_n(tmp_path):
    s = fake_session(tmp_path, sample())
    data, kept = fork_payload(s, 1)
    assert kept == 1
    assert data["messages"] == sample()[:7]            # sliced right before turn 2's message
    # past-the-end and huge both mean "everything"
    assert fork_payload(s, 2)[1] == 2
    assert fork_payload(s, 99)[1] == 2
    # with no completed turn there is nothing to fork
    empty = fake_session(tmp_path, [msg("user", sess_mod.STEERING_NOTE + "nope"),
                                    msg("assistant", "ok")])
    assert fork_payload(empty) == ({}, 0)


def test_fork_payload_carries_prompt_and_mode(tmp_path):
    class Held:
        pass
    s = fake_session(tmp_path, sample(), auto_override="all")
    extra = {"prompt_mode": "file", "prompt_text": "be brief"}
    s.__dict__["prompt_edit_extra"] = extra            # whatever save_extra reads back
    from hexiel import prompt_edit
    original = prompt_edit.save_extra
    prompt_edit.save_extra = lambda session: dict(extra)
    try:
        data, kept = fork_payload(s)
    finally:
        prompt_edit.save_extra = original
    assert data["auto_mode"] == "all"
    assert data["prompt_text"] == "be brief"


# ---------------------------------------------------------------- TUI end to end

def test_cmd_fork_swaps_and_replays(tmp_path):
    s = fake_session(tmp_path, sample(), id="orig12")

    async def go():
        app = make_app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            app.session = s
            app.cmd_fork("1")
            await pilot.pause()
            forked = app.session
            texts = [str(getattr(w, "visual", getattr(w, "source", "")))
                     for w in app.query_one("#chat").children]
            return app, forked, texts

    app, forked, texts = run(go())
    assert forked is not s and forked.id != "orig12"
    assert len(forked.messages) == 7                   # turn 1 + synthetic bridge, no turn 2
    assert "footer" not in json.dumps(forked.messages)
    # branch replays into the chat (the forked branch is the live one now)
    assert any("fix the widget" in t.replace("\n", " ") for t in texts)
    # both history files exist, the original keeps everything
    from hexiel.config import home_data_dir
    sdir = home_data_dir() / "sessions"
    orig = json.loads((sdir / "orig12.json").read_text())
    forked_file = json.loads((sdir / f"{forked.id}.json").read_text())
    assert len(orig["messages"]) == 10
    assert forked_file["id"] == forked.id


def test_cmd_fork_guards(tmp_path):
    s = fake_session(tmp_path, sample(), id="orig12")
    empty = fake_session(tmp_path, [], id="bare13")

    async def go():
        app = make_app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            app.session = s
            app.cmd_fork("banana")                     # bad arg: gentle usage note, no fork
            await pilot.pause()
            assert app.session is s
            app.session = empty
            app.cmd_fork("")                           # nothing completed: no fork
            await pilot.pause()
            return app

    app = run(go())
    assert app.session is empty
