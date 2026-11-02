"""Test-suite recognition, progress reading, the bash tool's waiting run, and the background wake."""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import clipboard, testrun
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, _run_bash, finished_for_wake, drain_finished, _TASKS


@pytest.mark.parametrize("cmd,label", [
    ("pytest -x", "pytest"), ("cd a && uv run python -m pytest tests", "pytest"), ("npm test", "npm test"),
    ("npm run test:unit", "npm run test:unit"), ("npx jest --ci", "jest"), ("go test ./...", "go test"),
    ("cargo nextest run", "cargo test"), ("dotnet test", "dotnet test"), ("mvn -q verify", "maven test"),
    ("./gradlew check", "gradle test"), ("bundle exec rspec", "rspec"), ("make test", "make test"),
    ("FOO=1 sudo -E dotnet test", "dotnet test"), ("swift test", "swift test"), ("ctest -j4", "ctest"),
    ("bun test", "bun test"), ("cypress run", "cypress"), ("playwright test", "playwright"),
    ("mix test", "mix test"), ("phpunit", "phpunit"), ("timeout 600 pytest", "pytest"),
])
def test_detects_frameworks(cmd, label):
    assert testrun.detect(cmd) == label


@pytest.mark.parametrize("cmd", ["git status", "cargo build", "make build", "grep pytest foo", "pip install pytest",
                                 "echo go test", "npm install", "cat tests/test_a.py", "ls tests"])
def test_ignores_other_commands(cmd):
    assert testrun.detect(cmd) == ""


def test_extra_patterns():
    assert testrun.detect("./ci/run-suite --fast", [r"^\./ci/run-suite"]) == "tests"
    assert testrun.detect("./ci/run-suite", ["(unclosed"]) == ""


def _run(cmd="pytest"):
    return testrun.Run(id="x", label="pytest", cmd=cmd, cwd="/tmp")


def test_explicit_percent_wins():
    r = _run()
    r.feed("tests/test_a.py ....                                    [ 40%]")
    assert r.fraction() == (0.4, "output")


def test_cargo_counts_against_announced_total():
    r = _run()
    r.feed("running 4 tests")
    for name in ("a", "b"):
        r.feed(f"test {name} ... ok")
    assert r.fraction() == (0.5, "output")
    r.feed("test c ... FAILED")
    assert r.failed == 1


def test_tap_plan():
    r = _run()
    r.feed("1..10")
    r.feed("ok 1 - a")
    r.feed("not ok 2 - b")
    assert r.fraction()[0] == 0.2 and r.failed == 1


def test_dots_use_previous_run_as_total():
    r = _run()
    r.prev = {"duration": 60, "units": 200, "kind": "dots"}
    r.feed("..........")
    frac, how = r.fraction()
    assert how == "history" and 0.04 < frac < 0.06


def test_falls_back_to_previous_duration_then_unknown():
    r = _run()
    assert r.fraction() == (None, "unknown")
    r.prev = {"duration": 100}
    r.started = time.time() - 50
    frac, how = r.fraction()
    assert how == "time" and 0.45 < frac < 0.55


def test_ansi_and_carriage_returns_ignored():
    r = _run()
    r.feed("\x1b[32m[ 10%]\x1b[0m\r\x1b[32m[ 55%]\x1b[0m")
    assert r.pct_out == 0.55


def test_finish_records_history_for_next_run():
    r = testrun.start("pytest -q", "/tmp/proj", "pytest")
    r.feed("....")
    r.finish(0)
    nxt = testrun.start("pytest -q", "/tmp/proj", "pytest")
    assert nxt.prev["units"] == 4 and nxt.prev["kind"] == "dots"
    nxt.finish(0)


def test_bar_and_line():
    assert testrun.bar(0.5, 10) == "█████░░░░░"
    assert len(testrun.bar(None, 10)) == 10
    snap = _run().snapshot()
    assert "pytest" in testrun.line(snap)


FAKE = r"^printf"          # lets a harmless printf stand in for a test framework


def _ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path), chain=Toolchain(), test_commands=(FAKE,))


def test_bash_waits_for_a_test_run_and_reports(tmp_path):
    out = _run_bash({"command": "printf '[ 50%%]\\n'; sleep 0.4; printf '[100%%]\\nok\\n'"}, _ctx(tmp_path))
    assert not out.is_error
    assert "tests: passed in" in out.text and "[100%]" in out.text
    assert not testrun.active()
    assert not any(t.wake and t.done and not t.reaped for t in _TASKS.values())   # nothing left to wake the model with


def test_failed_run_is_an_error_with_exit_code(tmp_path):
    out = _run_bash({"command": "printf 'x\\n'; exit 3"}, _ctx(tmp_path))
    assert out.is_error and "exit code 3" in out.text and "FAILED" in out.text


def test_background_run_wakes_instead_of_polling(tmp_path):
    drain_finished()
    out = _run_bash({"command": "printf 'a\\n'; sleep 0.3", "run_in_background": True}, _ctx(tmp_path))
    assert "Do NOT poll" in out.text and "woken" in out.text
    assert testrun.active()
    deadline = time.time() + 10
    notes = []
    while time.time() < deadline and not notes:
        time.sleep(0.2)
        notes = finished_for_wake()
    assert notes and "finished (exit code 0)" in notes[0] and "passed" in notes[0]
    assert finished_for_wake() == []


def test_tasks_tool_does_not_hand_over_output_of_running_suite(tmp_path):
    from hexiel.tools import _tasks_tool
    out = _run_bash({"command": "printf 'a\\n'; sleep 1.5", "run_in_background": True}, _ctx(tmp_path))
    tid = next(w for w in out.text.split() if w.startswith("bg-")).rstrip(";,.")
    polled = _tasks_tool({"op": "output", "id": tid}, _ctx(tmp_path))
    assert "do NOT poll" in polled.text
    time.sleep(2.0)
    drain_finished()


# ---------------------------------------------------------------- clipboard text

def test_clipboard_text_never_returns_image_bytes(monkeypatch):
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: "/bin/" + name if name == "wl-paste" else None)
    monkeypatch.setattr(clipboard, "_run", lambda cmd: b"\x89PNG\r\n\x1a\n\x00\x00")
    assert clipboard.clipboard_text() is None


def test_clipboard_text_reads_text_with_a_text_type(monkeypatch):
    calls = []
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: "/bin/" + name if name == "wl-paste" else None)
    monkeypatch.setattr(clipboard, "_run", lambda cmd: calls.append(cmd) or b"hello")
    assert clipboard.clipboard_text() == "hello"
    assert "--type" in calls[0] and "text" in calls[0]


def test_copy_text_without_helpers(monkeypatch):
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: None)
    assert clipboard.copy_text("x") is False
    assert "wl-clipboard" in clipboard.missing_helper_hint()
