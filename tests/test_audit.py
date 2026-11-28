"""token audit: carry cost, repeats, and the mask-old-results replay, on small hand-made conversations."""
import json

from hexiel import audit


def convo(results, extra_turns=0):
    """assistant call + tool result pairs; each result is (tool, args, text)."""
    msgs = [{"role": "user", "content": "go"}]
    for n, (tool, args, text) in enumerate(results):
        cid = f"c{n}"
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": cid, "name": tool, "arguments": args}]})
        msgs.append({"role": "tool", "tool_call_id": cid, "content": text})
    for _ in range(extra_turns):
        msgs.append({"role": "assistant", "content": "thinking"})
    return msgs


BIG = "x" * 4000      # 1000 tokens


def test_carry_is_size_times_the_requests_after_it():
    a = audit.analyze(convo([("read", {"path": "a"}, BIG)], extra_turns=4))
    assert a["by_tool"]["read"] == {"calls": 1, "tokens": 1000, "carry": 4000}
    assert a["requests"] == 5 and a["carry"] == 4000


def test_identical_refetch_is_counted_as_a_repeat_but_a_changed_result_is_not():
    same = audit.analyze(convo([("read", {"path": "a"}, BIG), ("read", {"path": "a"}, BIG)]))
    assert same["repeats"] == 1 and same["repeat_tokens"] == 1000
    changed = audit.analyze(convo([("read", {"path": "a"}, BIG), ("read", {"path": "a"}, BIG + "y" * 40)]))
    assert changed["repeats"] == 0


def test_small_results_are_never_masked():
    a = audit.analyze(convo([("bash", {"command": "ls"}, "a.py\nb.py")], extra_turns=30))
    assert a["masked"] == 0 and a["saved"] == 0


def test_replay_masks_old_big_results_and_charges_for_asking_again():
    quiet = audit.analyze(convo([("read", {"path": "a"}, BIG)], extra_turns=30), keep_recent=10)
    assert quiet["masked"] == 1
    assert quiet["masked_carry"] == 1000 * 10 + audit.STUB_TOKENS * 20      # full for 10 requests, then a stub
    assert quiet["saved"] == quiet["carry"] - quiet["masked_carry"] > 0
    msgs = convo([("read", {"path": "a"}, BIG)] + [("bash", {"command": "ls"}, "ok")] * 14 + [("read", {"path": "a"}, BIG)])
    again = audit.analyze(msgs, keep_recent=10)
    assert again["refetch"] == 1000                                        # it was hidden, then needed again
    assert again["saved"] == again["carry"] - again["masked_carry"] - 1000


def test_tool_calls_stored_as_a_json_string_still_parse():
    msgs = convo([("read", {"path": "a"}, BIG)])
    msgs[1]["tool_calls"] = json.dumps(msgs[1]["tool_calls"])
    assert audit.analyze(msgs)["by_tool"]["read"]["calls"] == 1


def test_render_and_main_over_files(tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"messages": convo([("read", {"path": "a"}, BIG)], extra_turns=30)}))
    (tmp_path / "broken.json").write_text("{nope")
    text = audit.main([str(f), str(tmp_path / "broken.json")])
    assert "1 session(s)" in text and "read" in text and "UPPER BOUND" in text
    assert "no tool results" in audit.main([str(tmp_path / "broken.json")])


def test_cli_flag(monkeypatch):
    from hexiel.main import parse_args
    monkeypatch.setattr("sys.argv", ["hexiel", "--audit"])
    assert parse_args().audit == []
