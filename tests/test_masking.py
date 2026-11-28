"""observation masking: old big tool results become stubs, in batches, never the ones that must stay."""
from types import SimpleNamespace

from hexiel import masking

BIG = "x" * 4000          # 1000 tokens


def convo(results, tail=0):
    msgs = [{"role": "user", "content": "go"}]
    for n, (tool, args, text) in enumerate(results):
        cid = f"c{n}"
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": cid, "name": tool, "arguments": args}]})
        msgs.append({"role": "tool", "tool_call_id": cid, "content": text})
    msgs += [{"role": "assistant", "content": "ok"} for _ in range(tail)]
    return msgs


def test_old_big_results_are_masked_once_enough_have_piled_up():
    msgs = convo([("read", {"path": f"f{n}.py"}, BIG) for n in range(19)])     # result n has 18-n requests after it
    assert masking.mask_in_place(msgs, keep=10, batch=8) == 8                  # n = 0..7 are older than 10 requests
    results = [m["content"] for m in msgs if m.get("role") == "tool"]
    assert results[0].startswith("[older read 'f0.py' result removed")
    assert "1000 tokens" in results[0] and "call read again" in results[0]
    assert all(t == BIG for t in results[8:])                                  # the recent ones stay whole


def test_it_waits_for_a_batch_so_the_cached_prefix_is_not_rewritten_every_request():
    msgs = convo([("read", {"path": "a"}, BIG)] * 3, tail=12)
    assert masking.mask_in_place(msgs, keep=10, batch=8) == 0
    assert all(m["content"] == BIG for m in msgs if m.get("role") == "tool")


def test_small_results_skills_and_the_todo_list_are_never_masked():
    msgs = convo([("bash", {"command": "ls"}, "a.py"), ("skill", {"name": "x"}, BIG), ("todo", {"action": "list"}, BIG),
                  ("read", {"path": "z"}, BIG)], tail=15)
    assert masking.mask_in_place(msgs, keep=10, batch=1) == 1
    kept = [m["content"] for m in msgs if m.get("role") == "tool"]
    assert kept[0] == "a.py" and kept[1] == BIG and kept[2] == BIG and kept[3].startswith("[older read")


def test_masking_is_idempotent_and_leaves_compaction_stamps_alone():
    msgs = convo([("read", {"path": "a"}, BIG), ("read", {"path": "b"}, "[prior tool output elided: ~4000 chars]")], tail=15)
    assert masking.mask_in_place(msgs, keep=10, batch=1) == 1
    assert masking.mask_in_place(msgs, keep=10, batch=1) == 0


def test_json_string_tool_calls_and_missing_ids_do_not_crash():
    import json
    msgs = convo([("read", {"path": "a"}, BIG)], tail=15)
    msgs[1]["tool_calls"] = json.dumps(msgs[1]["tool_calls"])
    msgs.insert(3, {"role": "tool", "tool_call_id": "ghost", "content": BIG})
    assert masking.mask_in_place(msgs, keep=10, batch=1) == 2


def test_stub_names_the_call_so_the_model_knows_what_to_rerun():
    assert "bash 'pytest -q'" in masking.stub("bash", {"command": "pytest -q"}, 321)
    assert masking.stub("tasks", None, 5).startswith("[older tasks result removed")


def test_the_setting_defaults_off_and_parses(tmp_path, monkeypatch):
    import hexiel.config as config
    s = config._settings_from({"mask_old_results": "on", "mask_keep": 6})
    assert s.mask_old_results == "on" and s.mask_keep == 6
    assert config._settings_from({}).mask_old_results == "off" and config.Settings().mask_old_results == "off"
    assert config._settings_from({"mask_keep": 1}).mask_keep == 3             # never below a few requests


def test_a_session_masks_between_requests_only_when_on(tmp_path):
    from hexiel import llm
    from hexiel.config import ModelConfig, Settings
    from hexiel.session import Session
    from hexiel.tooling import Toolchain
    from hexiel.tools import ToolContext, build_tools

    def run(mode):
        mc = ModelConfig(name="t", provider="openai", model="m", base_url="http://x", context_window=200_000)
        s = Session(mc, Settings(mask_old_results=mode), cwd=str(tmp_path), tools=build_tools(Toolchain()), system_prompt="sys")
        s.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
        s.messages = convo([("read", {"path": f"f{n}"}, BIG) for n in range(19)])
        seen = []

        class P:
            def stream(self, messages, *a, **k):
                seen.append([m.get("content") for m in messages if m.get("role") == "tool"])
                yield ("assistant", {"role": "assistant", "content": "done"})
                yield ("done", {"prompt_tokens": 1, "completion_tokens": 1})
        import hexiel.llm as llm_mod
        orig = llm_mod.make_provider
        llm_mod.make_provider = lambda *a, **k: P()
        try:
            s.run_turn("hi")
        finally:
            llm_mod.make_provider = orig
        return seen[0]

    on, off = run("on"), run("off")
    assert sum(1 for t in on if t.startswith("[older read")) == 8
    assert all(t == BIG for t in off)
