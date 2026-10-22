"""/remote-control: one chat shared by the terminal and a browser through Hexiel Remote."""

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import remote, remote_control
from hexiel.config import ModelConfig, Settings
from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, build_tools

aiohttp = pytest.importorskip("aiohttp")


def make_session(tmp_path) -> Session:
    s = Session(ModelConfig(name="t", provider="openai", model="m", base_url="http://127.0.0.1:9/v1"),
                Settings(), cwd=str(tmp_path), tools=build_tools(Toolchain()), system_prompt="t")
    s.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    return s


class Mirror:
    def __init__(self, viewers=True, answer=None, delay=0.0):
        self.seen, self.viewers, self.answer, self.delay, self.cancelled = [], viewers, answer, delay, False
        self.gate = threading.Event()

    def has_viewers(self): return self.viewers
    def on_event(self, event, *payload, **kw): self.seen.append(event)

    def ask_approval(self, *args):
        if self.answer is not None:
            time.sleep(self.delay)
            return self.answer
        self.gate.wait(5)                       # a browser nobody answers
        return "no"

    def cancel_approvals(self):
        self.cancelled = True
        self.gate.set()


def test_a_mirror_sees_every_event_the_terminal_sees(tmp_path):
    s = make_session(tmp_path)
    terminal = []
    s.on_event = lambda event, *p, **k: terminal.append(event)
    mirror = Mirror()
    s.add_mirror(mirror)
    s.on_event("note", "hi")
    s.on_event = lambda event, *p, **k: terminal.append("again:" + event)     # the TUI swaps handlers every turn
    s.on_event("text", "x")
    assert terminal == ["note", "again:text"] and mirror.seen == ["note", "text"]
    s.remove_mirror(mirror)
    s.on_event("note", "bye")
    assert mirror.seen == ["note", "text"]


def test_a_broken_mirror_never_breaks_the_chat(tmp_path):
    s = make_session(tmp_path)
    got = []
    s.on_event = lambda event, *p, **k: got.append(event)

    class Bad(Mirror):
        def on_event(self, *a, **k): raise RuntimeError("boom")
    s.add_mirror(Bad())
    s.on_event("note", "x")
    assert got == ["note"]


def test_the_browser_can_answer_an_approval_and_the_terminal_prompt_closes(tmp_path):
    s = make_session(tmp_path)
    closed = []
    s.dismiss_approval = lambda: closed.append(True)
    gate = threading.Event()
    s.on_approve = lambda *a: (gate.wait(5), "no")[1]          # the terminal modal, unanswered
    s.add_mirror(Mirror(answer="yes"))
    assert s.on_approve("bash", {"command": "ls"}) == "yes"
    assert closed == [True]
    gate.set()


def test_the_terminal_can_answer_first_and_the_browser_prompt_closes(tmp_path):
    s = make_session(tmp_path)
    s.on_approve = lambda *a: "always"
    mirror = Mirror()
    s.add_mirror(mirror)
    assert s.on_approve("bash", {}) == "always"
    assert mirror.cancelled


def test_a_mirror_with_nobody_watching_is_not_asked(tmp_path):
    s = make_session(tmp_path)
    s.on_approve = lambda *a: "yes"
    mirror = Mirror(viewers=False, answer="no")
    s.add_mirror(mirror)
    assert s.on_approve("bash", {}) == "yes" and not mirror.cancelled


def test_the_login_endpoint_never_crosses_the_tunnel():
    assert not remote.path_allowed("/api/remote/login")


def test_the_service_is_paused_and_restored(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path))
    monkeypatch.setattr(remote.sys, "platform", "linux")
    calls = []
    monkeypatch.setattr(remote, "service_active", lambda: True)
    monkeypatch.setattr(remote, "_run", lambda cmd: calls.append(cmd) or remote.subprocess.CompletedProcess(cmd, 0, "", ""))
    assert remote.pause_service() is True
    assert remote.pause_file().exists() and ["systemctl", "--user", "stop", "hexiel-remote"] in calls
    remote.resume_service()
    assert not remote.pause_file().exists() and ["systemctl", "--user", "start", "hexiel-remote"] in calls


def test_nothing_to_pause_when_the_service_is_not_running(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path))
    monkeypatch.setattr(remote, "service_active", lambda: False)
    assert remote.pause_service() is False and not remote.pause_file().exists()


def test_a_killed_chat_does_not_leave_the_service_paused(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path))
    started = []
    monkeypatch.setattr(remote, "resume_service", lambda: started.append(True))
    remote.pause_file().write_text("999999999")               # a pid that is not running
    remote.resume_if_orphaned()
    assert started == [True]
    started.clear()
    remote.pause_file().write_text("1")                       # init is alive: its owner still holds the connection
    remote.resume_if_orphaned()
    assert started == []


def test_remote_control_needs_a_login_first(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path))
    assert not remote_control.signed_in()
    with pytest.raises(RuntimeError, match="password"):
        remote_control.sign_in("a@b.co", "")
    remote.save_config({"device_token": "t"})
    assert remote_control.signed_in()


async def _end_to_end(tmp_path, monkeypatch):
    """A terminal chat turns remote control on; a fake relay then plays the browser: reads the chat and types into it."""
    from aiohttp import web
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(remote, "service_active", lambda: False)
    monkeypatch.setattr(remote, "_lock_handle", None)         # earlier tests may have taken the lock in this process
    session = make_session(tmp_path)
    session.messages = [{"role": "user", "content": "earlier question"}, {"role": "assistant", "content": "earlier answer"}]
    typed, got = [], {}

    class Driver:
        def busy(self): return False
        def submit(self, text): typed.append(text)

    async def relay_ws(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"t": "ws_open", "sid": "s1"})
        async for msg in ws:
            f = json.loads(msg.data)
            if f["t"] == "ws_msg" and "history" in f["data"] and "history" not in got:
                got["history"] = json.loads(f["data"])
                await ws.send_json({"t": "ws_msg", "sid": "s1", "data": json.dumps({"t": "send", "text": "from the phone"})})
            elif f["t"] == "ws_msg":
                got.setdefault("later", []).append(json.loads(f["data"]))
        return ws

    relay = web.Application()
    relay.router.add_get("/ws/device", relay_ws)
    rrunner = web.AppRunner(relay)
    await rrunner.setup()
    rsite = web.TCPSite(rrunner, "127.0.0.1", 0)
    await rsite.start()
    rport = rsite._server.sockets[0].getsockname()[1]
    remote.save_config({"relay": f"http://127.0.0.1:{rport}", "device_id": "d", "device_token": "t", "name": "x"})

    loop = asyncio.get_running_loop()
    note = await loop.run_in_executor(None, lambda: remote_control.start(session, driver=Driver(), say=lambda m: None))
    for _ in range(100):
        if typed:
            break
        await asyncio.sleep(0.05)
    assert remote_control.active(session)
    session.on_event("note", "the terminal did something")          # reaches the browser too
    for _ in range(100):
        if any(m.get("t") == "note" for m in got.get("later", [])):
            break
        await asyncio.sleep(0.05)
    off = await loop.run_in_executor(None, lambda: remote_control.stop(session))
    await rrunner.cleanup()
    return note, off, typed, got, session


def test_a_terminal_chat_can_be_driven_from_the_browser(tmp_path, monkeypatch):
    note, off, typed, got, session = asyncio.run(_end_to_end(tmp_path, monkeypatch))
    assert note.startswith("Remote control ON")
    texts = [i.get("text") for i in got["history"]["items"]]
    assert "earlier question" in texts and "earlier answer" in texts        # the browser sees the live conversation
    assert typed == ["from the phone"]                                       # and what it types goes to the terminal's chat
    assert any(m.get("t") == "note" and "terminal did something" in m["text"] for m in got["later"])
    assert off.startswith("Remote control OFF") and not remote_control.active(session)
    assert session.mirrors == []


def _tui(tmp_path):
    from hexiel.tui import HexielTUI
    return HexielTUI(make_session(tmp_path), [], {})


def test_slash_remote_control_asks_for_a_login_when_signed_out(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path / "data"))
    from hexiel.tui import RemoteLoginScreen

    async def go():
        app = _tui(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            app.submit_text("/remote-control")
            await pilot.pause()
            return isinstance(app.screen, RemoteLoginScreen)
    assert asyncio.run(go())


def test_the_terminal_chat_runs_a_turn_typed_in_the_browser(tmp_path, monkeypatch):
    from hexiel.tui import _RemoteDriver
    started = []

    async def go():
        app = _tui(tmp_path)
        monkeypatch.setattr(app, "_start_turn", lambda text: started.append(text))
        async with app.run_test(size=(100, 40)) as pilot:
            driver = _RemoteDriver(app)
            await asyncio.get_running_loop().run_in_executor(None, lambda: driver.submit("hello from the phone"))
            await pilot.pause()
            return driver.busy()
    assert asyncio.run(go()) is False and started == ["hello from the phone"]
