"""Automatic upkeep done in Python: todo tracking/checkpoints, durable-fact memories,
case-insensitive project instruction files."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import memory as memory_mod
from hexiel import todo as todo_mod
from hexiel.config import ModelConfig, Settings
from hexiel.prompt import _project_docs
from hexiel.session import Session


def fake_session(tmp_path, monkeypatch, steps):
    """A session whose model returns the given assistant messages in order and
    whose tools just echo — enough to drive the real turn loop."""
    s = Session(ModelConfig(name="t", provider="openai", model="m", base_url="http://x"),
                Settings(), str(tmp_path), [], "sys")
    replies = iter(steps)
    monkeypatch.setattr(Session, "_stream_assistant", lambda self, provider, defs: next(replies))
    monkeypatch.setattr(Session, "_execute_tool", lambda self, call: self.messages.append(
        {"role": "tool", "tool_call_id": call["id"], "content": "ok"}))
    monkeypatch.setattr("hexiel.llm.make_provider", lambda *a, **k: None)
    return s


def call(i, name="grep"):
    return {"id": f"c{i}", "name": name, "arguments": {"pattern": f"p{i}"}}


def test_multistep_request_is_tracked_and_finished(tmp_path, monkeypatch):
    steps = [{"role": "assistant", "content": "Looking.", "tool_calls": [call(1), call(2)]},
             {"role": "assistant", "content": "More.", "tool_calls": [call(3)]},
             {"role": "assistant", "content": "All fixed and tested."}]
    s = fake_session(tmp_path, monkeypatch, steps)
    s.run_turn("fix the flaky login test\nmore detail here")
    title, left_off, items = todo_mod.read_todos(str(tmp_path))
    assert [(i.state, i.text) for i in items] == [("x", "fix the flaky login test")]
    assert left_off.startswith("done: All fixed")


def test_short_requests_are_not_tracked(tmp_path, monkeypatch):
    steps = [{"role": "assistant", "content": "x", "tool_calls": [call(1)]},
             {"role": "assistant", "content": "answer"}]
    fake_session(tmp_path, monkeypatch, steps).run_turn("what is in README?")
    assert todo_mod.read_todos(str(tmp_path))[2] == []


def test_model_managed_todo_is_left_alone(tmp_path, monkeypatch):
    steps = [{"role": "assistant", "content": "", "tool_calls": [call(1, "todo"), call(2), call(3), call(4)]},
             {"role": "assistant", "content": "done"}]
    fake_session(tmp_path, monkeypatch, steps).run_turn("big refactor")
    assert todo_mod.read_todos(str(tmp_path))[2] == []        # harness didn't add its own item


def test_interrupted_task_stays_in_progress(tmp_path, monkeypatch):
    steps = [{"role": "assistant", "content": "Starting the migration.", "tool_calls": [call(1), call(2), call(3)]}]
    s = fake_session(tmp_path, monkeypatch, steps)        # 2nd model call raises StopIteration = crash
    try:
        s.run_turn("migrate the database")
    except (RuntimeError, StopIteration):
        pass
    _, left_off, items = todo_mod.read_todos(str(tmp_path))
    assert [(i.state, i.text) for i in items] == [("~", "migrate the database")]
    assert left_off.startswith("interrupted: Starting the migration")


def test_durable_facts_become_deduped_memory(tmp_path):
    summary = """## Current State
done
## Durable Facts
- Backend tests run with `./mvnw -q test` from backend/
- The user wants Angular signals, not RxJS subjects, for new state
## Next Steps
- none
"""
    facts = memory_mod.parse_durable_facts(summary)
    assert len(facts) == 2
    assert memory_mod.save_auto_facts(str(tmp_path), facts) == 2
    assert memory_mod.save_auto_facts(str(tmp_path), facts + ["New fact about Docker compose files"]) == 1
    mem_dir = memory_mod.project_memory_dir(str(tmp_path))
    body = (mem_dir / "auto-facts.md").read_text()
    assert body.count("./mvnw -q test") == 1 and "Docker compose" in body
    assert "auto-facts.md" in (mem_dir / "MEMORY.md").read_text()


def test_project_docs_any_case_and_todo_pointer(tmp_path):
    (tmp_path / "agents.md").write_text("# rules\nalways run the e2e suite")
    (tmp_path / "TODO.md").write_text("- [ ] a\n- [ ] b\n")
    docs = _project_docs(str(tmp_path))
    assert "always run the e2e suite" in docs and "agents.md" in docs
    assert "TODO.md" in docs and "(2 lines)" in docs


def test_quick_question_keeps_existing_checkpoint(tmp_path, monkeypatch):
    todo_mod.auto_track(str(tmp_path), "long running migration")
    todo_mod.auto_checkpoint(str(tmp_path), "halfway through step 3")
    steps = [{"role": "assistant", "content": "x", "tool_calls": [call(1)]},
             {"role": "assistant", "content": "it's in README"}]
    fake_session(tmp_path, monkeypatch, steps).run_turn("where is the README?")
    assert todo_mod.read_todos(str(tmp_path))[1] == "halfway through step 3"
