"""#29 /watch — AI:/AI? comments in any file react exactly once when saved; the
marker parsing works across languages, unchanged files cost nothing, and the
TUI toggle submits watched comments as turns on the UI thread."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hexiel.watch import Watcher, comment_markers, TICK


def test_comment_markers_across_languages():
    py = "# AI: add the retry loop\nx = 1\n   #   AI?  cache this?"
    assert comment_markers(py) == [(1, ":", "add the retry loop"), (3, "?", "cache this?")]
    js = "// AI? slash question\n// AI:  rename handleLogin"
    out = comment_markers(js)
    assert out[0][2] == "slash question" and out[1][2] == "rename handleLogin"
    html_c = "<!-- AI: move the nav left -->\n/* AI? is this class used? */"
    out = comment_markers(html_c)
    assert out[0][2] == "move the nav left" and out[1][2] == "is this class used?"
    # prose keeps quiet; a marker needs the colon directly on the letters AI
    assert comment_markers("the AI was here\nSAYING AI IS FINE\nai: lowercase stays quiet") == []
    # a bare AI? with nothing after it doesn't fire
    assert comment_markers("# AI?") == []


def test_watcher_reacts_once_per_saved_comment(tmp_path):
    fired = []
    w = Watcher(str(tmp_path), lambda p, n, k, t: fired.append((p.name, n, k, t)), tick=0.05)
    notes = tmp_path / "notes.py"
    notes.write_text("# AI: hello\n")
    (tmp_path / ".git").mkdir()                      # skipped subtree keeps a stray marker
    (tmp_path / ".git" / "config.py").write_text("# AI: should never fire\n")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG AI: never")
    assert w.scan_once() == 1                        # the marker fires
    assert fired == [("notes.py", 1, ":", "hello")]
    assert w.scan_once() == 0                        # unchanged file: silent
    # rewording the comment fires again
    notes.write_text("# AI: hello\n# AI: hello again\n")
    assert w.scan_once() == 1
    assert fired[-1] == ("notes.py", 2, ":", "hello again")   # line 1 was already reacted to
    # deleting the file costs nothing; rewriting the SAME text stays quiet —
    # reword (or move) the comment to make hexiel react again
    notes.unlink()
    assert w.scan_once() == 0
    notes.write_text("# AI: hello\n")
    assert w.scan_once() == 0
    notes.write_text("# AI: hello, differently\n")
    assert w.scan_once() == 1


def test_watcher_prompt_names_the_place(tmp_path):
    w = Watcher(str(tmp_path), lambda *a: None)
    prompt = w.prompt_for(tmp_path / "src" / "app.py", 41, "?", "keep the cache?")
    assert "app.py:41" in prompt and "keep the cache?" in prompt
    assert "answer this question" in prompt
    instr = w.prompt_for(tmp_path / "app.py", 2, ":", "split it in two")
    assert "do this" in instr and "answer" not in instr


def test_watcher_thread_and_stop(tmp_path):
    w = Watcher(str(tmp_path), lambda *a: None, tick=0.05)
    w.start_background()
    time.sleep(0.05)
    w.stop()
    assert w._stop.is_set()


def test_watcher_survives_a_bad_callback(tmp_path):
    calls = []
    w = Watcher(str(tmp_path), lambda p, n, k, t: calls.append(t) or 1 / 0, tick=0.05)
    (tmp_path / "a.py").write_text("# AI: first\n")
    assert w.scan_once() == 1                        # the callback raised; the watcher moved on
    (tmp_path / "b.py").write_text("# AI: second\n")
    assert w.scan_once() == 1


# ---------------------------------------------------------------- TUI wiring

def _ui_texts(app) -> str:
    return "\n".join(str(getattr(w, "visual", getattr(w, "source", "")))
                     for w in app.query_one("#chat").children)


def test_cmd_watch_toggles_and_submits(tmp_path):
    from tests.test_tui import make_app, run
    seen = []

    async def go():
        app = make_app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            app._start_turn = lambda prompt: seen.append(prompt)   # no real turn thread in tests
            app.cmd_watch("")
            await pilot.pause()
            assert app._watch is not None
            active = app._watch
            assert Path(active.root) == tmp_path
            # a comment fires now: reaches the turn prompt on the UI thread
            app._watch_submit(active, tmp_path / "note.py", 3, ":", "polish the footer")
            await pilot.pause()
            app.cmd_watch("off")
            await pilot.pause()
            texts = _ui_texts(app)
            return app, active, texts

    app, active, texts = run(go())
    assert app._watch is None                        # off: nothing left running
    assert len(seen) == 1
    assert "note.py:3" in seen[0] and "polish the footer" in seen[0]
    assert "watching" in texts and "watch off" in texts
    active.stop()


def test_cmd_watch_rejects_bad_dir(tmp_path):
    from tests.test_tui import make_app, run

    async def go():
        app = make_app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            app.cmd_watch("nowhere/at/all")
            await pilot.pause()
            return app, _ui_texts(app)

    app, texts = run(go())
    assert app._watch is None
    assert "no such directory" in texts


def test_stale_watcher_never_submits(tmp_path):
    """A /watch off between the tick and the callback: the stale comment never
    reaches the session."""
    from tests.test_tui import make_app, run
    seen = []

    async def go():
        app = make_app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            app._start_turn = lambda prompt: seen.append(prompt)
            app.cmd_watch("")
            active = app._watch
            app._watch = None                        # /watch off happened just now
            app._watch_submit(active, tmp_path / "f.py", 1, ":", "late note")
            await pilot.pause()
            return seen

    assert run(go()) == []                           # no turn for a dead watcher's comment
