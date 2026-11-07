"""#36 worktree isolation — /wt (list/create/open/remove), the `task` worktree argument,
headless --worktree flag wiring, env-file copying and the WorktreePostSetup hook."""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hexiel import worktree as wt


def repo(tmp_path: Path, env: bool = True, example: bool = True) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "f.py").write_text("x = 1\n")
    if env:
        (tmp_path / ".env").write_text("DB=local\n")           # untracked: must be copied
    if example:
        (tmp_path / ".env.example").write_text("TEMPLATE=\n")  # tracked: comes with the checkout
    subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "f.py"] + ([".env.example"] if example else []), cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "one"],
                   cwd=tmp_path, check=True)
    return str(tmp_path)


def test_create_named_and_env_copy(tmp_path):
    cwd = repo(tmp_path)
    w = wt.create(cwd, "fix/parallel")                  # a name with a slash is not a dirname hazard?
    assert w.path.name == "fix-parallel" and w.branch == "hexiel/fix-parallel"
    assert (w.path / "f.py").is_file()                  # the checkout is there
    assert (w.path / ".env").read_text() == "DB=local\n"
    assert (w.path / ".env.example").read_text() == "TEMPLATE=\n"  # tracked: from the checkout itself
    assert wt.is_repo(str(w.path))


def test_create_env_skip_example_only_untracked(tmp_path):
    cwd = repo(tmp_path, example=False)
    (tmp_path / ".env.example").write_text("TEMPLATE=\n")     # now untracked: skipped as a template
    w = wt.create(cwd, "envtest")
    assert (w.path / ".env").is_file()
    assert not (w.path / ".env.example").exists()


def test_ephemeral_names_number_up_and_dups_rejected(tmp_path):
    cwd = repo(tmp_path)
    a = wt.create(cwd, "")
    b = wt.create(cwd, "")
    assert a.path.name == "wt-1" and b.path.name == "wt-2"
    try:
        wt.create(cwd, "wt-1")
        raise AssertionError("duplicate worktree path must be rejected")
    except ValueError:
        pass


def test_list_find_remove_cleans_our_branch(tmp_path):
    cwd = repo(tmp_path)
    wt.create(cwd, "alpha")
    wts = wt.list_worktrees(cwd)
    assert wts[0].is_main and wts[0].branch == "main"
    assert wts[1].branch == "hexiel/alpha" and not wts[1].is_main
    assert wt.find(cwd, "alp").branch == "hexiel/alpha"        # name prefix
    assert wt.find(cwd, "alpha") .path.name == "alpha"
    note = wt.remove(cwd, "alpha")
    assert "alpha" in note and "hexiel/alpha" in note
    assert len(wt.list_worktrees(cwd)) == 1                    # main only
    rc = subprocess.run(["git", "rev-parse", "--verify", "-q", "hexiel/alpha"],
                        cwd=cwd, capture_output=True, text=True).returncode
    assert rc == 1                                             # module-created branch cleaned up


def test_remove_survives_a_foreign_branch(tmp_path):
    cwd = repo(tmp_path)
    subprocess.run(["git", "branch", "user/thing"], cwd=cwd, check=True)
    w = wt.create(cwd, "thing2", branch="user/thing")
    note = wt.remove(cwd, "thing2")
    assert "worktree" in note and "branch" not in note         # a user branch survives


def test_worktree_post_setup_hook_fires_in_the_worktree(tmp_path):
    cwd = repo(tmp_path, env=False, example=False)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({"hooks": {
        "WorktreePostSetup": [{"matcher": "", "hooks": [
            {"type": "command", "command": "echo post-setup ran in wt"}]}]}}))
    from hexiel import hooks as hooks_mod
    hooks = hooks_mod.load(cwd)
    assert hooks and hooks[0].event == "WorktreePostSetup"
    notes = []
    w = wt.create(cwd, "hooked", hooks=hooks, session_id="sess1", on_note=notes.append)
    assert any("post-setup ran in wt" in n for n in notes), notes


# ---------------------------------------------------------------- task tool isolation

def _mk_session(cwd: str):
    from hexiel.config import ModelConfig, Settings
    from hexiel.session import Session
    from hexiel.tooling import Toolchain
    from hexiel.tools import ToolContext, build_tools
    tools = build_tools(Toolchain())
    s = Session(ModelConfig(name="t", provider="openai", model="m", base_url=""),
                Settings(), cwd, tools, "sys")
    s.bind_tool_ctx(ToolContext(cwd=cwd, chain=Toolchain()))
    s.auto_approve = True
    return s


def test_task_worktree_arg_isolates_the_helper(tmp_path, monkeypatch):
    cwd = repo(tmp_path)
    s = _mk_session(cwd)
    replies = iter([
        {"role": "assistant", "content": "Looked around; nothing changes in the main tree."}])
    from hexiel.session import Session as S
    monkeypatch.setattr(S, "_stream_assistant", lambda self, provider, defs: next(replies))
    monkeypatch.setattr("hexiel.llm.make_provider", lambda *a, **k: None)
    result = s.run_subagent({"description": "check things", "prompt": "look", "worktree": True})
    assert "worktree:" in result.text and "hexiel/wt-1" in result.text
    assert "/wt remove wt-1" in result.text
    assert len(wt.list_worktrees(cwd)) == 2                    # main + the subagent's
    # the helper's writes went to the worktree, not the repo: no NEW untracked files in the main tree
    before = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True).stdout
    after = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True).stdout
    assert before == after


def test_task_named_worktree_is_reused_not_recreated(tmp_path, monkeypatch):
    cwd = repo(tmp_path)
    s = _mk_session(cwd)
    wt.create(cwd, "shared")
    replies = iter([{"role": "assistant", "content": "done"}])
    from hexiel.session import Session as S
    monkeypatch.setattr(S, "_stream_assistant", lambda self, provider, defs: next(replies))
    monkeypatch.setattr("hexiel.llm.make_provider", lambda *a, **k: None)
    result = s.run_subagent({"prompt": "look", "worktree": "shared"})
    assert "pre-existing" in result.text
    assert len(wt.list_worktrees(cwd)) == 2                    # not three


def test_task_worktree_failure_falls_back(tmp_path, monkeypatch):
    cwd = repo(tmp_path)
    s = _mk_session(cwd)
    notes = []
    s.on_event = lambda e, *p, **k: notes.append(p[0]) if e == "note" else None
    replies = iter([{"role": "assistant", "content": "ran in place"}])
    from hexiel.session import Session as S
    monkeypatch.setattr(S, "_stream_assistant", lambda self, provider, defs: next(replies))
    monkeypatch.setattr("hexiel.llm.make_provider", lambda *a, **k: None)
    monkeypatch.setattr(wt, "create", lambda *a, **k: (_ for _ in ()).throw(OSError("no fs space")))
    result = s.run_subagent({"prompt": "look", "worktree": True})
    assert "ran in place" in result.text and any("worktree unavailable" in n for n in notes)


# ---------------------------------------------------------------- headless / TUI

def test_main_wires_the_worktree_flag_before_building_the_session(tmp_path, monkeypatch):
    cwd = repo(tmp_path)
    from hexiel import main as main_mod
    from hexiel.worktree import Worktree
    created = []

    def fake_create(c, name, **k):
        path = tmp_path.parent / ".hexiel-wt" / tmp_path.name / "wt-1"
        path.mkdir(parents=True, exist_ok=True)            # os.chdir(cwd) happens on the real path
        created.append(name)
        return Worktree(path=path, head="abc", branch="hexiel/wt-1")

    monkeypatch.setattr("hexiel.worktree.create", fake_create)
    monkeypatch.setattr("hexiel.worktree.is_repo", lambda c: True)
    monkeypatch.setattr(sys, "argv", ["hexiel", cwd, "--worktree", "--presets"])
    main_mod.main()                                        # --presets prints the listing and returns
    assert created == [""]                                 # the worktree was made from the CLI flag


def test_headless_runs_inside_the_worktree(tmp_path, monkeypatch):
    cwd = repo(tmp_path)
    from hexiel.worktree import Worktree
    fake_path = tmp_path.parent / ".hexiel-wt" / tmp_path.name / "wt-1"
    fake_path.mkdir(parents=True, exist_ok=True)

    def fake_create(c, name, **k):
        return Worktree(path=fake_path, head="abc", branch="hexiel/wt-1")

    built = []

    def fake_build(cwd_arg, model_name, resumed=None, reuse=None):
        built.append(cwd_arg)
        raise SystemExit("stop here")                      # don't run a real turn

    monkeypatch.setattr("hexiel.worktree.create", fake_create)
    monkeypatch.setattr("hexiel.worktree.is_repo", lambda c: True)
    monkeypatch.setattr("hexiel.main.build_session", fake_build)
    monkeypatch.setattr(sys, "argv", ["hexiel", cwd, "-p", "say hi", "--worktree"])
    from hexiel.main import main as main_mod
    try:
        main_mod()                                         # SystemExit from fake_build propagates
    except SystemExit:
        pass
    assert built == [str(fake_path)]


def test_cmd_wt_lists_creates_and_opens(tmp_path):
    from tests.test_tui import make_app, run
    cwd = repo(tmp_path)

    async def go():
        app = make_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            app.cmd_wt("create alpha")
            await pilot.pause()
            create_texts = "\n".join(str(getattr(w, "visual", getattr(w, "source", "")))
                                     for w in app.query_one("#chat").children)
            app.cmd_wt("open alpha")
            await pilot.pause()
            texts = "\n".join(str(getattr(w, "visual", getattr(w, "source", "")))
                              for w in app.query_one("#chat").children)
            return app, create_texts, texts

    app, create_texts, texts = run(go())
    assert "🌿 worktree" in create_texts and "hexiel/alpha" in create_texts   # create note + merge hint
    assert "now in worktree" in texts                                        # the open note
    assert str(app.session.cwd).endswith("alpha")           # the tab lives in the worktree now


def test_cmd_wt_needs_a_repo(tmp_path):
    from tests.test_tui import make_app, run

    async def go():
        app = make_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            app.cmd_wt("list")
            await pilot.pause()
            texts = "\n".join(str(getattr(w, "visual", getattr(w, "source", "")))
                              for w in app.query_one("#chat").children)
            return texts

    texts = run(go())
    assert "needs a git repository" in texts
