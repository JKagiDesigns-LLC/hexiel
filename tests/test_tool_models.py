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
    monkeypatch.setattr(planner, "complete", lambda session, mc, system, user, max_tokens=0: (calls.append((mc.name, user)) or (summary, {})))
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
    ("https://api.openai.com/v1", "openai", False), ("", "anthropic", False), ("https://ollama.com/v1", "openai", False)])
def test_which_models_count_as_local(url, provider, local):
    assert modelbusy.is_local(ModelConfig(name="m", provider=provider, model="x", base_url=url)) is local


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
