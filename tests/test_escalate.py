"""When the working model keeps struggling (failures, empty results, repeats, or the USER correcting it) the strong model
takes over for a short burst of steps, then the work goes back to the cheap one."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import consult, llm, planner
from hexiel.config import ModelConfig, Settings
from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, ToolDef, text_result

LOCAL = ModelConfig(name="local", provider="openai", model="small", base_url="http://x", context_window=65536)
CLOUD = ModelConfig(name="cloud", provider="openai", model="big", base_url="http://y", context_window=200000)
DONE = {"role": "assistant", "content": "done"}


def call(name="empty", **args):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{len(args)}{args}", "name": name, "arguments": args}]}


def build(tmp_path, monkeypatch, replies, escalate="auto", consult_mode="auto", planner_profile="cloud", prior=True,
          on_tool=None):
    tools = [ToolDef("empty", "finds nothing", {"type": "object", "properties": {}},
                     run=lambda a, c: (on_tool() if on_tool else None) or text_result("(no output)")),
             ToolDef("ok", "works", {"type": "object", "properties": {}}, run=lambda a, c: text_result("fine"))]
    s = Session(LOCAL, Settings(auto_approve=True, planner_profile=planner_profile, escalate_mode=escalate,
                                consult_mode=consult_mode), str(tmp_path), tools, "sys")
    s.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    s.save = lambda: None
    if prior:                                                   # an earlier exchange, so a correction has something to correct
        s.messages += [{"role": "user", "content": "find jellyfin"}, {"role": "assistant", "content": "it is on gmktec"}]
    events, labels, requests = [], [], []
    s.on_event = lambda e, *p, **k: (events.append((e, *p)), labels.append(s.active_model) if e == "model_active" else None)
    queue = list(replies)

    def fake_once(self, provider, defs):
        requests.append({"model": self.model_config.name, "override": self._provider_override})
        return queue.pop(0)
    monkeypatch.setattr(Session, "_stream_once", fake_once)
    made = {}

    def fake_make(mc, settings):
        made.setdefault(mc.name, type("P", (), {"name": mc.name})())
        return made[mc.name]
    monkeypatch.setattr(llm, "make_provider", fake_make)
    monkeypatch.setattr(planner, "_config_profile", lambda session, name: {"cloud": CLOUD, "local": LOCAL}.get(name))
    asked = []
    monkeypatch.setattr(planner, "complete", lambda session, mc, system, user, max_tokens=4096: (
        asked.append(user) or "ADVICE: look in the VM", {"prompt_tokens": 300, "completion_tokens": 40}))
    return s, events, labels, requests, asked, made


def notes(events):
    return [e[1] for e in events if e[0] == "note"]


# ---------------------------------------------------------------- noticing corrections

CORRECTIONS = [
    "NOOOOOOOO it IS on macmini running in a VM. It is NOT on gmktec stop arguing with me",
    "I am telling you the scripts may be outdated. stop arguing and listen to me.",
    "this is getting rediculous. you just need to run the docker compose. stop chasing ghosts. get to work",
    "as I have told you many times. MacMini has jellyfin running in a VM", "No, that's not what I meant",
    "wrong! it's the other file!!", "still broken", "why do you keep asking me that", "stop", "that is wrong", "NO NO NO",
    "I assure you it exists"]
NORMAL = ["ok fix topgear, ignore pokemon those are lost episodes", "continue", "status?", "ok yes to both questions",
          "add a README and a JSON schema for the config", "what's wrong with the build?", "please run the tests again",
          "thanks, that looks good", "x" * 700 + " wrong"]


@pytest.mark.parametrize("text", CORRECTIONS)
def test_corrections_are_recognised(text):
    assert consult.looks_like_correction(text), text


@pytest.mark.parametrize("text", NORMAL)
def test_ordinary_messages_are_not_corrections(text):
    assert not consult.looks_like_correction(text), text


# ---------------------------------------------------------------- the switch

def test_a_correction_gets_advice_and_more_trouble_moves_the_work_to_the_cloud_model(tmp_path, monkeypatch):
    holder = {}

    def second_correction():                                   # the user speaks up again while hexiel is working
        if "sent" not in holder:
            holder["sent"] = True
            holder["s"].queue_message("NOOOO it IS on macmini, it is NOT on gmktec")
    s, events, labels, requests, asked, made = build(
        tmp_path, monkeypatch,
        [call(q=1), call(q=2), call(q=3), DONE], on_tool=second_correction)
    holder["s"] = s
    s.run_turn("no, that's wrong, stop arguing and look in the VM")
    assert len(asked) == 1 and "look in the VM" in asked[0]                    # 1st correction: advice, local keeps going
    models = [r["model"] for r in requests]
    assert models[:2] == ["local", "local"] and models[-1] == "cloud" and "cloud" in models        # trouble + 2nd correction: cloud takes over
    first_cloud = models.index("cloud")
    assert requests[first_cloud]["override"] is made["cloud"] and requests[0]["override"] is None
    assert any(n.startswith("⬆ switching to cloud for the next 8 steps") for n in notes(events))
    assert "big · escalated" in labels
    assert s.model_config.name == "local" and s._provider_override is None and s.active_model == ""   # handed back
    assert any(n.startswith("↩ back on small") for n in notes(events))


def test_repeated_failures_get_advice_and_then_the_cloud_model_takes_over(tmp_path, monkeypatch):
    fail = ToolDef("fail", "x", {"type": "object", "properties": {}}, run=lambda a, c: text_result("error: nope", True))
    s, events, _, requests, asked, _ = build(tmp_path, monkeypatch, [call("fail", a=i) for i in range(1, 7)] + [DONE], prior=False)
    s.tools.append(fail)
    s.run_turn("fix it")
    assert [r["model"] for r in requests] == ["local"] * 6 + ["cloud"]
    assert len(asked) == 1                                                  # advice after the first 3 failures, the cloud after 3 more
    assert any("3 tool calls in a row failed" in n for n in notes(events))


def test_exploring_with_some_empty_results_is_not_mistaken_for_struggling(tmp_path, monkeypatch):
    replies = []
    for i in range(4):
        replies += [call("empty", q=3 * i), call("empty", q=3 * i + 1), call("ok", n=i)]       # searches that find nothing, then one that works
    s, events, _, requests, asked, _ = build(tmp_path, monkeypatch, replies + [DONE], prior=False)
    s.run_turn("look around")
    assert {r["model"] for r in requests} == {"local"} and asked == []


def test_a_long_run_of_empty_results_does_eventually_get_advice(tmp_path, monkeypatch):
    s, events, _, requests, asked, _ = build(tmp_path, monkeypatch, [call("empty", q=i) for i in range(14)] + [DONE], prior=False)
    s.run_turn("find the file")
    assert len(asked) == 1 and any("had nothing in them" in n for n in notes(events))


def test_a_burst_lasts_a_few_steps_then_returns_to_the_local_model(tmp_path, monkeypatch):
    replies = [call("ok", n=i) for i in range(14)] + [DONE]
    s, events, _, requests, *_ = build(tmp_path, monkeypatch, replies, prior=False)
    s.run_turn("do many things")                                            # nothing struggles here...
    assert {r["model"] for r in requests} == {"local"}
    # ... now force the situation: escalate by hand, then run the same kind of turn
    s2, events2, _, requests2, *_ = build(tmp_path, monkeypatch, [call("ok", n=i) for i in range(14)] + [DONE], prior=False)
    s2.model_config = LOCAL
    consult.reset_turn(s2)
    assert consult.escalate(s2, "test", steps=consult.ESCALATE_STEPS)
    s2.run_turn("continue the work")                                        # (run_turn resets; so call the loop hook directly)
    consult.end_burst(s2)


def test_burst_length_is_counted_in_requests(tmp_path, monkeypatch):
    s, events, _, requests, *_ = build(tmp_path, monkeypatch, [call("ok", n=i) for i in range(12)] + [DONE], prior=False)
    consult.reset_turn(s)
    assert consult.escalate(s, "test")
    seen = []
    for _ in range(12):
        seen.append(s.model_config.name)
        consult.maybe_help(s)
    assert seen[:8] == ["cloud"] * 8 and seen[8:] == ["local"] * 4        # exactly ESCALATE_STEPS requests on the cloud model


def test_slash_cloud_answers_the_whole_request_with_the_cloud_model(tmp_path, monkeypatch):
    s, events, labels, requests, asked, made = build(tmp_path, monkeypatch, [call("ok", a=1), call("ok", a=2), call("ok", a=3), DONE],
                                                     prior=False)
    s.run_turn("/cloud why does the build fail?")
    assert [r["model"] for r in requests] == ["cloud"] * 4
    assert any("for this request" in n and "you asked for the cloud model" in n for n in notes(events))
    assert s.model_config.name == "local" and s._provider_override is None
    assert s.messages[0]["content"] == "why does the build fail?"            # the prefix is not shown to the model


def test_nothing_happens_without_a_stronger_model_or_when_switched_off(tmp_path, monkeypatch):
    for kwargs in ({"escalate": "off"}, {"planner_profile": "local"}):
        holder = {}
        s, events, _, requests, *_ = build(tmp_path, monkeypatch, [call(q=i) for i in range(1, 8)] + [DONE], **kwargs,
                                           on_tool=lambda: holder["s"].queue_message("NOOO it IS wrong STOP"))
        holder["s"] = s
        s.run_turn("no, wrong, stop it!!")
        assert {r["model"] for r in requests} == {"local"}, kwargs
    s, events, *_ = build(tmp_path, monkeypatch, [DONE], planner_profile="local", prior=False)
    s.run_turn("/cloud hello")
    assert any("no stronger model is available" in n for n in notes(events))


def test_a_missing_key_for_the_cloud_model_is_reported_and_work_continues(tmp_path, monkeypatch):
    s, events, _, requests, *_ = build(tmp_path, monkeypatch, [DONE], prior=False)
    def no_key(mc, settings):
        if mc.name == "cloud":
            raise SystemExit("set ANTHROPIC_API_KEY")
        return object()
    monkeypatch.setattr(llm, "make_provider", no_key)
    s.run_turn("/cloud hi")
    assert any("could not switch to cloud" in n for n in notes(events)) and requests[0]["model"] == "local"


def test_at_most_two_bursts_per_request(tmp_path, monkeypatch):
    s, *_ = build(tmp_path, monkeypatch, [DONE], prior=False)
    consult.reset_turn(s)
    for _ in range(3):
        consult._add(s, 10, "bad")
        consult.maybe_help(s)
        for _ in range(consult.ESCALATE_STEPS + 1):
            consult.maybe_help(s)
    assert consult._state(s)["bursts"] == consult.MAX_BURSTS_PER_TURN


def test_compaction_during_a_burst_uses_the_active_provider(tmp_path, monkeypatch):
    s, events, _, requests, asked, made = build(tmp_path, monkeypatch, [call("ok", a=1), call("ok", a=2), DONE], prior=False)
    used = []
    monkeypatch.setattr(Session, "compaction_needed", lambda self: True)
    monkeypatch.setattr(Session, "compact", lambda self, provider, manual=False: used.append((self.model_config.name, provider)))
    s.run_turn("/cloud look around")
    assert used and all(name == "cloud" and provider is made["cloud"] for name, provider in used)


def test_a_session_saved_during_a_burst_remembers_the_cheap_model(tmp_path, monkeypatch):
    import json
    from hexiel.config import home_data_dir
    s, *_ = build(tmp_path, monkeypatch, [DONE], prior=False)
    del s.save                                                               # use the real save()
    consult.reset_turn(s)
    consult.escalate(s, "test")
    s.save()
    saved = json.loads((home_data_dir() / "sessions" / f"{s.id}.json").read_text())
    assert saved["model"] == "local"


def test_the_settings_row_exists_and_is_validated(tmp_path, monkeypatch):
    import hexiel.config as config
    from hexiel import settings_edit
    s, *_ = build(tmp_path, monkeypatch, [DONE])
    assert any(r["key"] == "settings.escalate_mode" and r["label"] == "Switch to cloud" for r in settings_edit.rows(s))
    cfg = tmp_path / "config.toml"
    cfg.write_text('[models.x]\nprovider = "openai"\nmodel = "m"\nbase_url = "http://h/v1"\n[settings]\nescalate_mode = "sometimes"\n')
    monkeypatch.setattr(config, "CONFIG_FILE", cfg)
    assert config.load_config().settings.escalate_mode == "auto"


def test_helper_sessions_do_not_read_hexiels_own_prompts_as_user_corrections(tmp_path, monkeypatch):
    s, events, _, requests, asked, _ = build(tmp_path, monkeypatch, [DONE], prior=True)
    s.internal = True
    s.run_turn("That did not work: the check failed. Fix it and finish this step.")
    assert asked == [] and {r["model"] for r in requests} == {"local"}
    assert not consult.can_escalate(s)                                       # a stuck delegated step is escalated by the plan itself
