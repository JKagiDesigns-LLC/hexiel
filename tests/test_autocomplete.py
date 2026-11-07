"""Autocomplete: path tokens (@, ~, ./, quoted paths with spaces, relative with a slash),
first-qualifier Tab behavior, live path menu in the TUI, and the Ctrl+R history picker."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hexiel import complete as C
from hexiel.tui import CommandMenu
from tests.test_tui import make_app, run


def make_tree(tmp_path) -> str:
    for d in ("src", "src/utils", "src_modules", "hello world"):
        (tmp_path / d).mkdir()
    for f in ("src/main.py", "src/main_test.py", "README.md", "readme.txt",
              "hello world/shot.png", "hello world/note.png"):
        (tmp_path / f).write_text("x")
    (tmp_path / ".hexiel").mkdir()
    (tmp_path / ".secrets.txt").write_text("x")
    return str(tmp_path)


# ---------------------------------------------------------------- token detection

def test_find_path_token_rules(tmp_path):
    tmp = str(tmp_path)
    # triggers
    assert C.find_path_token("look at ./src/ma") == "./src/ma"
    assert C.find_path_token("compare @shot") == "@shot"
    assert C.find_path_token("see ~/Doc") == "~/Doc"
    assert C.find_path_token('edit "hello world/sh') == '"hello world/sh'
    assert C.find_path_token("fix src/utils/li") == "src/utils/li"
    # quiet on prose
    assert C.find_path_token("the config is bad") is None
    assert C.find_path_token("don't want that") is None
    assert C.find_path_token("") is None
    assert C.completions("@", tmp) is None              # a bare @: no cwd listing spam


def test_quoted_paths_may_span_spaces(tmp_path):
    assert C.find_path_token('open "my docs/n') == '"my docs/n'
    # fresh prose quotes don't eat Tab: no / inside yet
    assert C.find_path_token('"my doc') is None
    assert C.find_path_token('"my doc/s') == '"my doc/s'
    # an @ before the quote keeps the mention
    assert C.find_path_token('@"hello world/n') == '@"hello world/n'


# ---------------------------------------------------------------- candidates + Tab steps

def test_candidates_filter_and_sort(tmp_path):
    tmp = make_tree(tmp_path)
    names = [c["name"] for c in C.completions("./src/m", tmp)]
    assert names == ["main.py", "main_test.py"]        # files after/no dirs listed here
    dirs = [c["name"] for c in C.completions("./", tmp)]
    assert "src/" not in dirs and "src" in dirs        # dirs come bare; the TUI notes them
    assert "hello world" in dirs and "secrets" not in " ".join(dirs)  # dotfiles stay hidden
    dot = [c["name"] for c in C.completions("./.h", tmp)]
    assert dot == [".hexiel"]                          # a dot query sees dot entries


def test_first_qualifier_and_menu(tmp_path):
    tmp = make_tree(tmp_path)
    r = C.autocomplete_path("read the ./re", tmp)      # README.md + readme.txt share 'readme.'
    assert r["value"] == "./README.", r                # first candidate's casing wins
    r = C.autocomplete_path("./src/main", tmp)         # main.py + main_test.py share only 'main'
    assert "completions" in r and len(r["completions"]) == 2, r   # qualifier fully typed: menu
    # a unique file completes all the way, quoting/space included
    r = C.autocomplete_path('"hello world/sh', tmp)
    assert r["value"] == '"hello world/shot.png" '
    assert r["finish"] is True
    # fully typed single file: Tab just finishes (closes quote / space)
    r = C.autocomplete_path("./src/main.py", tmp)
    assert C.finish(r["token"], r["completions"][0]) == "./src/main.py "


def test_finish_rules(tmp_path):
    assert C.finish('./src/util', {"name": "utils", "is_dir": True}) == './src/utils/'
    assert C.finish('"hello world/sh', {"name": "shot.png", "is_dir": False}) == '"hello world/shot.png" '
    assert C.finish('@sho', {"name": "shot.png", "is_dir": False}) == '@shot.png '
    assert C.finish('@"hello world/sh', {"name": "shot.png", "is_dir": False}) == '@"hello world/shot.png" '
    # an unquoted single token only exists without spaces, but wrap spaces if they sneak in
    assert C.finish('./hello world/sh', {"name": "shot.png", "is_dir": False}) == '"./hello world/shot.png" '


# ---------------------------------------------------------------- the TUI

def test_live_path_menu_and_tab_accept(tmp_path):
    async def go():
        tmp = make_tree(tmp_path)
        app = make_app(Path(tmp))
        async with app.run_test(size=(100, 30)) as pilot:
            for key in "./r":
                await pilot.press(key)
            await pilot.pause()
            menu = app.query_one(CommandMenu)
            shown = [v for _, v in menu._items] if menu.display else []
            await pilot.press("down")          # pick readme.txt
            await pilot.press("tab")
            await pilot.pause()
            return shown, app.query_one("#prompt").value

    shown, value = run(go())
    assert any("readme" in s.lower() for s in shown), shown
    assert value == "./readme.txt "


def test_enter_sends_through_a_live_path_menu(tmp_path):
    async def go():
        tmp = make_tree(tmp_path)
        app = make_app(Path(tmp))
        called = []
        app._start_turn = lambda prompt: called.append(prompt)
        async with app.run_test(size=(100, 30)) as pilot:
            for key in "./ma":
                await pilot.press(key)
            await pilot.pause()
            await pilot.press("enter")         # Enter must send, not complete
            await pilot.pause()
            return app.query_one("#prompt").value, called

    value, called = run(go())
    assert value == ""
    assert called == ["./ma"]                  # sent as typed (Tab is the completion key)


def test_history_picker_ctrl_r(tmp_path):
    async def go():
        app = make_app(tmp_path)
        app._history = ["first idea", "run the login tests now"]
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("ctrl+r")
            await pilot.pause()
            picked = [p for p in app.screen_stack
                      if getattr(p, "__class__", None).__name__ == "HistoryPicker"]
            assert picked, "picker should be open"
            await pilot.press("l", "o", "g")   # filter to the tests entry
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            return app.query_one("#prompt").value

    value = run(go())
    assert value == "run the login tests now"


def test_history_picker_says_no_when_empty(tmp_path):
    async def go():
        app = make_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("ctrl+r")
            await pilot.pause()
            names = [getattr(s, "__class__", None).__name__ for s in app.screen_stack]
            await pilot.press("escape")
            await pilot.pause()
            return [n for n in names if n == "HistoryPicker"]

    assert run(go()) == []                     # nothing to recall: no picker opened
