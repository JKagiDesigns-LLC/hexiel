"""Per-tool models (a small model digests big tool output) and the 'another window is using this model' notice."""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import digest, modelbusy, planner
from hexiel.config import ModelConfig, Settings
from hexiel.tools import ToolResult
from tests.test_remote_control import make_session

SMALL = ModelConfig(name="small", provider="openai", model="tiny", base_url="http://127.0.0.1:11434/v1")


def session_with(tmp_path, monkeypatch, mapping, summary="the file defines foo() on line 12 and bar() on line 40", **kw):
    s = make_session(tmp_path)
    s.settings = Settings(tool_models=mapping, **kw)
    s.model_config = ModelConfig(name="big", provider="openai", model="large", base_url="http://127.0.0.1:11434/v1")
    s.__dict__["_turn_request"] = "where is foo defined?"
    calls = []
    monkeypatch.setattr(planner, "_config_profile", lambda session, name: SMALL if name == "small" else None)
    monkeypatch.setattr(planner, "complete", lambda session, mc, system, user, max_tokens=0, budget=None: (calls.append((mc.name, user)) or (summary, {})))
    events = []
    s.on_event = lambda e, *p, **k: events.append((e, *p))
    return s, calls, events


CALL = {"id": "c1", "name": "read", "arguments": {"path": "big.py"}}


def test_aliases_and_unsupported_tools(tmp_path):
    st = Settings(tool_models={"search": "small", "read": "small", "write": "small", "bash": "small", "grep": ""})
    assert digest.mapping(st) == {"web_search": "small", "read": "small"}          # authoring tools are never delegated


def test_a_big_output_is_summarised_by_the_tools_model(tmp_path, monkeypatch):
    s, calls, events = session_with(tmp_path, monkeypatch, {"read": "small"})
    raw = "x = 1\n" * 2000
    out = digest.maybe_digest(s, CALL, ToolResult(text=raw))
    assert out.text.startswith("[summary by small of 12,000 characters") and "foo() on line 12" in out.text
    assert "full=true" in out.text and len(out.text) < len(raw)
    assert calls[0][0] == "small" and "where is foo defined?" in calls[0][1]          # it is told what the request is
    assert any(e[0] == "note" and "summarised by small" in e[1] for e in events)


@pytest.mark.parametrize("result,args", [
    (ToolResult(text="short"), {}),                                                  # small outputs pass through
    (ToolResult(text="x" * 9000, is_error=True), {}),                                # errors are never hidden
    (ToolResult(text="x" * 9000), {"full": True}),                                   # the model asked for the raw text
])
def test_these_outputs_are_left_alone(tmp_path, monkeypatch, result, args):
    s, calls, _ = session_with(tmp_path, monkeypatch, {"read": "small"})
    assert digest.maybe_digest(s, {**CALL, "arguments": {"path": "a", **args}}, result) is result and calls == []


def test_unmapped_unknown_or_same_model_do_nothing(tmp_path, monkeypatch):
    big = ToolResult(text="x" * 9000)
    s, calls, _ = session_with(tmp_path, monkeypatch, {"grep": "small"})
    assert digest.maybe_digest(s, CALL, big) is big                                  # read is not mapped
    s, calls, _ = session_with(tmp_path, monkeypatch, {"read": "ghost"})
    assert digest.maybe_digest(s, CALL, big) is big                                  # unknown profile
    s, calls, _ = session_with(tmp_path, monkeypatch, {"read": "small"})
    s.model_config = SMALL
    assert digest.maybe_digest(s, CALL, big) is big and calls == []                  # already on that model


def test_a_failing_helper_falls_back_to_the_raw_output(tmp_path, monkeypatch):
    s, calls, events = session_with(tmp_path, monkeypatch, {"read": "small"})
    monkeypatch.setattr(planner, "complete", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("server down")))
    big = ToolResult(text="x" * 9000)
    assert digest.maybe_digest(s, CALL, big) is big
    assert any("couldn't summarise" in str(e) for e in events)


def test_a_useless_summary_is_not_used(tmp_path, monkeypatch):
    s, *_ = session_with(tmp_path, monkeypatch, {"read": "small"}, summary="x" * 8000)
    big = ToolResult(text="x" * 9000)
    assert digest.maybe_digest(s, CALL, big) is big


def test_mapped_tools_get_an_optional_full_argument(tmp_path):
    defs = [{"name": "read", "description": "", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}},
            {"name": "bash", "description": "", "parameters": {"type": "object", "properties": {}}}]
    out = digest.add_full_param(defs, Settings(tool_models={"read": "small"}))
    assert "full" in out[0]["parameters"]["properties"] and "full" not in out[1]["parameters"]["properties"]
    assert "full" not in defs[0]["parameters"]["properties"]                         # the originals are not mutated
    assert digest.add_full_param(defs, Settings()) is defs


def test_the_whole_path_through_the_session(tmp_path, monkeypatch):
    s, calls, _ = session_with(tmp_path, monkeypatch, {"read": "small"})
    (tmp_path / "big.py").write_text("".join(f"line_{i} = {i}\n" for i in range(800)))
    s.save = lambda: None
    s._execute_tool({"id": "c1", "name": "read", "arguments": {"path": "big.py"}})
    content = next(m for m in s.messages if m["role"] == "tool")["content"]
    assert content.startswith("[summary by small") and calls
    s.messages.clear()
    s._execute_tool({"id": "c2", "name": "read", "arguments": {"path": "big.py", "full": True}})
    assert "line_799 = 799" in next(m for m in s.messages if m["role"] == "tool")["content"]


def test_tool_models_are_read_from_the_config(tmp_path, monkeypatch):
    import hexiel.config as config
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('default = "a"\n[models.a]\nprovider = "openai"\nmodel = "m"\nbase_url = "http://x"\n'
                        '[settings]\ndigest_min_chars = 1200\n[tool_models]\nread = "a"\nsearch = "a"\n')
    monkeypatch.setattr(config, "CONFIG_FILE", cfg_file)
    cfg = config.load_config()
    assert cfg.settings.tool_models == {"read": "a", "search": "a"} and cfg.settings.digest_min_chars == 1200


# ---------------------------------------------------------------- busy notice

@pytest.mark.parametrize("url,provider,local", [
    ("http://localhost:11434/v1", "openai", True), ("http://192.168.3.10:11434/v1", "openai", True),
    ("http://gmktec:11434/v1", "openai", True), ("http://100.64.1.2:11434/v1", "openai", True),
    ("https://api.openai.com/v1", "openai", False), ("http://localhost:11434/v1", "openai:cloud", False), ("", "anthropic", False), ("https://ollama.com/v1", "openai", False)])
def test_which_models_count_as_local(url, provider, local):
    model = "glm-5.3-flash:cloud" if provider == "openai:cloud" else "x"          # Ollama Cloud models go through localhost but run elsewhere
    provider = provider.split(":")[0]
    assert modelbusy.is_local(ModelConfig(name="m", provider=provider, model=model, base_url=url)) is local


def test_a_second_request_on_the_same_local_model_is_told_it_may_wait(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path))
    said = []
    first = modelbusy.enter(SMALL, said.append, "window 1")
    assert said == [] and first.waiting == 0
    second = modelbusy.enter(SMALL, said.append, "window 2")
    assert second.waiting == 1 and "busy with 1 other request" in said[0] and "small" in said[0]
    first.release()
    third = modelbusy.enter(SMALL, said.append, "window 3")
    assert third.waiting == 1                                  # only the second is still using it
    second.release(); third.release()
    assert modelbusy.others(SMALL) == 0
    assert modelbusy.enter(SMALL, said.append).waiting == 0


def test_dead_and_stale_markers_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path))
    modelbusy.folder().mkdir(parents=True)
    key = modelbusy.model_key(SMALL)
    (modelbusy.folder() / f"{key}.dead.json").write_text(json.dumps({"pid": 999999999, "token": "dead", "started": 10 ** 12}))
    (modelbusy.folder() / f"{key}.old.json").write_text(json.dumps({"pid": os.getpid(), "token": "old", "started": 1}))
    assert modelbusy.others(SMALL) == 0
    assert list(modelbusy.folder().glob("*.json")) == []       # and they were cleaned up


def test_cloud_models_are_never_tracked(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path))
    cloud = ModelConfig(name="c", provider="anthropic", model="x")
    slot = modelbusy.enter(cloud, lambda t: pytest.fail("no notice for a cloud model"))
    assert slot.path is None and not (tmp_path / "busy").exists()


def test_the_busy_marker_is_gone_after_a_request_even_when_it_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path))
    s = make_session(tmp_path)
    s.model_config = SMALL
    during = []

    class Failing:
        def stream(self, *a, **k):
            during.append(len(list(modelbusy.folder().glob("*.json"))))
            raise RuntimeError("model fell over")
    with pytest.raises(RuntimeError):
        s._stream_once(Failing(), [])
    assert during == [1] and list(modelbusy.folder().glob("*.json")) == []


# ---------------------------------------------------------------- lists, tables and per-tool limits

def test_rules_accept_a_name_a_list_or_a_table():
    st = Settings(tool_models={"read": "a", "grep": ["a", "b"], "search": {"models": ["c"], "min_chars": 800, "timeout": 30},
                               "web_fetch": {"model": "d", "max_input": 6000}, "write": "a", "glob": [], "list": {"min_chars": 5}})
    rs = digest.rules(st)
    assert rs["read"] == {"models": ["a"], "min_chars": None, "max_input": None, "timeout": None, "max_tokens": None}
    assert rs["grep"]["models"] == ["a", "b"]
    assert rs["web_search"] == {"models": ["c"], "min_chars": 800, "max_input": None, "timeout": 30, "max_tokens": None}
    assert rs["web_fetch"]["models"] == ["d"] and rs["web_fetch"]["max_input"] == 6000
    assert set(rs) == {"read", "grep", "web_search", "web_fetch"}              # write, empty lists and tables without a model are dropped
    assert digest.mapping(st)["grep"] == "a"


def test_junk_limits_are_ignored():
    rs = digest.rules(Settings(tool_models={"read": {"model": "a", "min_chars": "lots", "max_input": -5, "timeout": True}}))
    assert rs["read"] == {"models": ["a"], "min_chars": None, "max_input": None, "timeout": None, "max_tokens": None}


def test_per_tool_thresholds(tmp_path, monkeypatch):
    s, calls, _ = session_with(tmp_path, monkeypatch, {"read": {"model": "small", "min_chars": 6000, "max_input": 20000}})
    assert digest.maybe_digest(s, CALL, ToolResult(text="x" * 5000)).text == "x" * 5000 and calls == []        # under this tool's minimum
    assert digest.maybe_digest(s, CALL, ToolResult(text="x" * 30000)).text == "x" * 30000 and calls == []      # over its maximum: raw
    out = digest.maybe_digest(s, CALL, ToolResult(text="x" * 10000))
    assert out.text.startswith("[summary by small") and len(calls) == 1


def test_a_per_tool_timeout_overrides_the_global_one(tmp_path, monkeypatch):
    s, calls, _ = session_with(tmp_path, monkeypatch, {"read": {"model": "small", "timeout": 7}}, digest_timeout=90)
    budgets = []
    monkeypatch.setattr(planner, "complete", lambda session, mc, system, user, max_tokens=0, budget=None: (budgets.append(budget) or ("short digest", {})))
    digest.maybe_digest(s, CALL, ToolResult(text="x" * 9000))
    assert budgets == [7.0]


def test_the_first_candidate_that_is_not_busy_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path / "data"))
    pi = ModelConfig(name="pi", provider="openai", model="tiny", base_url="http://192.168.4.2:11434/v1")
    gmk = ModelConfig(name="gmk", provider="openai", model="mid", base_url="http://192.168.3.10:11434/v1")
    s, *_ = session_with(tmp_path, monkeypatch, {"read": ["pi", "gmk"]})
    monkeypatch.setattr(planner, "_config_profile", lambda session, name: {"pi": pi, "gmk": gmk}.get(name))
    assert digest.model_for(s, "read").name == "pi"
    held = modelbusy.enter(pi)                                   # something else is using the Pi
    try:
        assert digest.model_for(s, "read").name == "gmk"
        held2 = modelbusy.enter(gmk)
        assert digest.model_for(s, "read").name == "pi"          # all busy: the first choice, which will queue
        held2.release()
    finally:
        held.release()
    assert digest.model_for(s, "read").name == "pi"


def test_unusable_candidates_are_skipped(tmp_path, monkeypatch):
    keyed = ModelConfig(name="keyed", provider="openai", model="m", base_url="https://api.example.com/v1", api_key_env="NOPE_KEY_X")
    s, *_ = session_with(tmp_path, monkeypatch, {"read": ["ghost", "keyed", "small"]})
    monkeypatch.setattr(planner, "_config_profile", lambda session, name: {"keyed": keyed, "small": SMALL}.get(name))
    monkeypatch.delenv("NOPE_KEY_X", raising=False)
    assert digest.model_for(s, "read").name == "small"
    s2, *_ = session_with(tmp_path, monkeypatch, {"read": ["ghost"]})
    assert digest.model_for(s2, "read") is None


def test_inline_tables_load_from_toml(tmp_path, monkeypatch):
    import hexiel.config as config
    f = tmp_path / "config.toml"
    f.write_text('default = "a"\n[models.a]\nprovider = "openai"\nmodel = "m"\nbase_url = "http://x"\n'
                 '[settings]\ndigest_timeout = 30\n[tool_models]\nread = { models = ["a", "b"], min_chars = 1500, max_input = 6000, timeout = 45, max_tokens = 200 }\ngrep = "a"\n')
    monkeypatch.setattr(config, "CONFIG_FILE", f)
    st = config.load_config().settings
    rs = digest.rules(st)
    assert rs["read"] == {"models": ["a", "b"], "min_chars": 1500, "max_input": 6000, "timeout": 45, "max_tokens": 200} and st.digest_timeout == 30
    assert rs["grep"]["models"] == ["a"]


def test_a_slow_helper_is_asked_for_a_short_digest(tmp_path, monkeypatch):
    s, calls, _ = session_with(tmp_path, monkeypatch, {"read": {"model": "small", "max_tokens": 150}})
    seen = {}
    monkeypatch.setattr(planner, "complete", lambda session, mc, system, user, max_tokens=0, budget=None:
                        (seen.update(system=system, max_tokens=max_tokens) or ("short digest", {})))
    digest.maybe_digest(s, CALL, ToolResult(text="x" * 9000))
    assert seen["max_tokens"] == 150 and "Under 105 words" in seen["system"]
    s2, *_ = session_with(tmp_path, monkeypatch, {"read": "small"})
    monkeypatch.setattr(planner, "complete", lambda session, mc, system, user, max_tokens=0, budget=None:
                        (seen.update(system=system, max_tokens=max_tokens) or ("short digest", {})))
    digest.maybe_digest(s2, CALL, ToolResult(text="x" * 9000))
    assert seen["max_tokens"] == digest.MAX_OUTPUT_TOKENS and "Under 350 words" in seen["system"]


# ---------------------------------------------------------------- falling back to the next helper

def two_helpers(tmp_path, monkeypatch, behave):
    pi = ModelConfig(name="pi", provider="openai", model="tiny", base_url="http://192.168.4.2:11434/v1")
    flash = ModelConfig(name="flash", provider="openai", model="fast", base_url="https://cloud.example/v1")
    s, calls, events = session_with(tmp_path, monkeypatch, {"read": ["pi", "flash"]})
    monkeypatch.setattr(planner, "_config_profile", lambda session, name: {"pi": pi, "flash": flash}.get(name))

    def complete(session, mc, system, user, max_tokens=0, budget=None):
        calls.append(mc.name)
        return behave(mc.name)
    monkeypatch.setattr(planner, "complete", complete)
    return s, calls, events


def test_a_failing_first_helper_hands_over_to_the_next(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path / "data"))

    def behave(name):
        if name == "pi":
            raise ConnectionRefusedError("pi is down")
        return "the cloud model's digest", {}
    s, calls, events = two_helpers(tmp_path, monkeypatch, behave)
    out = digest.maybe_digest(s, CALL, ToolResult(text="x" * 9000))
    assert calls == ["pi", "flash"] and out.text.startswith("[summary by flash") and "the cloud model's digest" in out.text
    notes = [e[1] for e in events if e[0] == "note"]
    assert any("pi couldn't summarise" in n and "trying the next model" in n for n in notes)
    assert digest.stats(s)["fallbacks"] == 1 and digest.stats(s)["digested"] == 1


def test_a_helper_that_just_failed_is_skipped_for_a_while(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path / "data"))
    s, calls, _ = two_helpers(tmp_path, monkeypatch, lambda name: (_ for _ in ()).throw(OSError("down")) if name == "pi" else ("ok digest", {}))
    digest.maybe_digest(s, CALL, ToolResult(text="x" * 9000))
    calls.clear()
    digest.maybe_digest(s, CALL, ToolResult(text="y" * 9000))                  # a different output (not cached)
    assert calls == ["flash"]                                                  # the Pi isn't tried again straight away
    s.__dict__["_digest_down"]["pi"] -= digest.DOWN_SECONDS + 1                # …but is retried once the quiet period passes
    calls.clear()
    digest.maybe_digest(s, CALL, ToolResult(text="z" * 9000))
    assert calls[0] == "pi"


def test_when_every_helper_fails_the_raw_output_is_used(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path / "data"))
    s, calls, events = two_helpers(tmp_path, monkeypatch, lambda name: (_ for _ in ()).throw(TimeoutError("slow")))
    raw = ToolResult(text="x" * 9000)
    assert digest.maybe_digest(s, CALL, raw) is raw and calls == ["pi", "flash"]
    notes = [e[1] for e in events if e[0] == "note"]
    assert notes[-1].endswith("using it as is") and digest.stats(s)["fallbacks"] == 2
    assert digest.maybe_digest(s, CALL, ToolResult(text="w" * 9000)).text == "w" * 9000 and calls == ["pi", "flash"]   # both just failed: not retried


def test_a_cached_digest_remembers_who_wrote_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path / "data"))
    s, calls, events = two_helpers(tmp_path, monkeypatch, lambda name: ("a fine digest", {}))
    first = digest.maybe_digest(s, CALL, ToolResult(text="x" * 9000))
    second = digest.maybe_digest(s, {**CALL, "id": "c2"}, ToolResult(text="x" * 9000))
    assert first.text == second.text and second.text.startswith("[summary by pi") and calls == ["pi"]
