"""Self-heal: isolated workspace, review screen, keep/send decisions.

The model's fix is simulated; the git workspace, change detection, review
screen and 'keep' application run for real against a scratch install."""

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import contribute as C
from hexiel.config import ModelConfig, Settings
from hexiel.session import Session
from hexiel.tui import HexielTUI, ReviewScreen


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def scratch_install(tmp_path, monkeypatch):
    """A fake git-checkout install of hexiel with two package files + a test."""
    root = tmp_path / "install"
    (root / "hexiel").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "hexiel" / "a.py").write_text("A = 1\n")
    (root / "hexiel" / "b.py").write_text("B = 1\n")
    (root / "tests" / "test_a.py").write_text("def test(): pass\n")
    git(root, "init", "-q")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "init", "--allow-empty")
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "files")
    monkeypatch.setattr(C, "INSTALL_ROOT", root)
    monkeypatch.setattr(C, "PACKAGE_DIR", root / "hexiel")
    return root


def test_workspace_changes_and_keep(scratch_install):
    ws = C.prepare("t1")
    (ws.path / "hexiel" / "a.py").write_text("A = 2\n")
    (ws.path / "hexiel" / "new.py").write_text("N = 1\n")
    (ws.path / "hexiel" / "b.py").unlink()
    found = {c.path: c for c in C.changes(ws)}
    assert {p: c.status for p, c in found.items()} == {
        "hexiel/a.py": "modified", "hexiel/new.py": "added", "hexiel/b.py": "deleted"}
    assert "+A = 2" in found["hexiel/a.py"].diff
    # the install is untouched until keep
    assert (scratch_install / "hexiel" / "a.py").read_text() == "A = 1\n"
    C.keep(ws, [found["hexiel/a.py"], found["hexiel/b.py"]])          # new.py left out
    assert (scratch_install / "hexiel" / "a.py").read_text() == "A = 2\n"
    assert not (scratch_install / "hexiel" / "b.py").exists()
    assert not (scratch_install / "hexiel" / "new.py").exists()
    C.discard(ws)
    assert not ws.path.exists()


def test_send_requires_a_token(scratch_install, monkeypatch):
    monkeypatch.setattr(C, "load_token", lambda: "")
    ws = C.prepare("t2")
    (ws.path / "hexiel" / "a.py").write_text("A = 3\n")
    with pytest.raises(C.GitlabError, match="setup-gitlab"):
        C.send(ws, C.changes(ws), "t", "b")
    C.discard(ws)


def test_tui_heal_flow_review_and_keep(scratch_install, tmp_path, monkeypatch):
    """/heal → workspace session → (simulated) fix → review screen → exclude one
    file → keep → only the chosen file reaches the install; session restored."""
    project = tmp_path / "my-project"
    project.mkdir()

    def fake_turn(self, user_text):                 # the "model" makes a two-file fix
        assert "SELF-HEALING hexiel" in user_text and str(self.cwd) in user_text
        (Path(self.cwd) / "hexiel" / "a.py").write_text("A = 42\n")
        (Path(self.cwd) / "hexiel" / "b.py").write_text("B = 42\n")
        self.on_event("turn_end")
    monkeypatch.setattr(Session, "_turn_body", fake_turn)
    restarts = []
    monkeypatch.setattr(HexielTUI, "_offer_restart", lambda self, v, h="": restarts.append(h))

    async def go():
        mc = ModelConfig(name="t", provider="openai", model="m", base_url="")
        app = HexielTUI(Session(mc, Settings(), str(project), [], "sys"), [], set())
        async with app.run_test(size=(140, 44)) as pilot:
            app.submit_text("/heal the status line crashes")
            for _ in range(60):                      # workspace prep + turn + diff run in threads
                await pilot.pause(0.1)
                if isinstance(app.screen, ReviewScreen):
                    break
            assert isinstance(app.screen, ReviewScreen), type(app.screen).__name__
            in_heal_cwd = app.session.cwd
            files = [c.path for c in app.screen.changes]
            await pilot.press("down", "space")        # exclude b.py
            await pilot.press("k")                    # keep just for me
            for _ in range(40):
                await pilot.pause(0.1)
                if restarts:
                    break
            return in_heal_cwd, files, app.session.cwd
    heal_cwd, files, cwd_after = asyncio.run(go())
    assert "heal" in heal_cwd and files == ["hexiel/a.py", "hexiel/b.py"]
    assert cwd_after == str(project)                               # user's session restored
    assert (scratch_install / "hexiel" / "a.py").read_text() == "A = 42\n"
    assert (scratch_install / "hexiel" / "b.py").read_text() == "B = 1\n"   # excluded
    assert restarts == ["self-heal fix applied"]


def test_web_heal_flow_review_and_keep(scratch_install, tmp_path, monkeypatch):
    """Browser: /heal → isolated fix → heal_review over the WebSocket → keep one file."""
    import json
    aiohttp = pytest.importorskip("aiohttp")
    from aiohttp import web
    from hexiel import updater
    from hexiel.webui import make_app

    project = tmp_path / "proj"
    project.mkdir()

    def fake_turn(self, user_text):
        if "SELF-HEALING" in user_text:
            (Path(self.cwd) / "hexiel" / "a.py").write_text("A = 7\n")
            (Path(self.cwd) / "hexiel" / "b.py").write_text("B = 7\n")
        self.on_event("turn_end")
    monkeypatch.setattr(Session, "_turn_body", fake_turn)
    monkeypatch.setattr(updater, "restart", lambda *a, **k: None)
    session = Session(ModelConfig(name="t", provider="openai", model="m", base_url=""),
                      Settings(), str(project), [], "sys")

    async def go():
        runner = web.AppRunner(make_app(session))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
                    await ws.receive_str()
                    await ws.send_str(json.dumps({"t": "send", "text": "/heal the status line crashes"}))
                    review = None
                    for _ in range(60):
                        data = json.loads(await asyncio.wait_for(ws.receive_str(), 10))
                        if data["t"] == "heal_review":
                            review = data
                            break
                async with client.post(f"http://127.0.0.1:{port}/api/heal/decide",
                                       json={"choice": "keep", "files": ["hexiel/a.py"]}) as resp:
                    result = await resp.json()
        finally:
            await runner.cleanup()
        return review, result
    review, result = asyncio.run(go())
    assert [f["path"] for f in review["files"]] == ["hexiel/a.py", "hexiel/b.py"]
    assert "+A = 7" in review["files"][0]["diff"]
    assert result["ok"] and result["restarting"]
    assert (scratch_install / "hexiel" / "a.py").read_text() == "A = 7\n"
    assert (scratch_install / "hexiel" / "b.py").read_text() == "B = 1\n"      # not selected
