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


def test_a_service_paused_by_an_older_version_is_brought_back(tmp_path, monkeypatch):
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
    assert remote_control.active(session), note
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


async def _fake_relay(handler, status=None):
    """A relay whose /ws/device runs `handler(request, ws)`, or refuses the handshake with `status`."""
    from aiohttp import web

    async def device_ws(request):
        if status:
            return web.json_response({"error": "nope"}, status=status)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await handler(request, ws)
        return ws
    app = web.Application()
    app.router.add_get("/ws/device", device_ws)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


class IdleDriver:
    def __init__(self): self.working = False
    def busy(self): return self.working
    def submit(self, text): pass


async def _session_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(remote.Tunnel, "STATUS_EVERY", 0.05)
    session = make_session(tmp_path)
    session.messages = [{"role": "user", "content": "  fix   the flaky test  "}]
    stopped, seen, driver = [], {}, IdleDriver()
    session.stop = lambda: stopped.append(True)

    async def relay(request, ws):
        seen.setdefault("conns", []).append(1)          # End must not be followed by a reconnect
        seen["query"] = dict(request.query)
        seen["hello"] = json.loads((await ws.receive()).data)
        driver.working = True                                  # a turn starts: the page learns of it
        seen["status"] = json.loads((await ws.receive(timeout=3)).data)
        await ws.send_json({"t": "stop"})
        await ws.send_json({"t": "run", "id": "r1", "prompt": "never in a chat"})
        await asyncio.sleep(0.2)
        await ws.send_json({"t": "end"})
        await ws.receive()                                     # the chat lets go
    runner, url = await _fake_relay(relay)
    remote.save_config({"relay": url, "device_id": "d", "device_token": "t", "name": "x"})
    notes = []
    loop = asyncio.get_running_loop()
    note = await loop.run_in_executor(None, lambda: remote_control.start(session, driver=driver, say=notes.append))
    for _ in range(100):
        if not remote_control.active(session):
            break
        await asyncio.sleep(0.05)
    await runner.cleanup()
    return note, seen, stopped, notes, session


def test_a_chat_is_its_own_session_and_the_account_page_can_stop_and_end_it(tmp_path, monkeypatch):
    note, seen, stopped, notes, session = asyncio.run(_session_lifecycle(tmp_path, monkeypatch))
    assert note.startswith("Remote control ON")
    assert seen["query"]["kind"] == "chat" and len(seen["query"]["session"]) == 16
    hello = seen["hello"]
    assert seen["conns"] == [1], seen
    assert hello["ui"] == "terminal" and hello["title"] == "fix the flaky test" and hello["busy"] is False
    assert "models" not in hello                               # only the service offers models for automations
    assert seen["status"] == {"t": "status", "title": "fix the flaky test", "busy": True}
    assert stopped == [True]                                   # Stop on the page stops the turn here
    assert not remote_control.active(session) and session.mirrors == []     # End turned remote control off
    assert any("turned off from remote.hexiel.tech" in n for n in notes)


async def _refused(tmp_path, monkeypatch, status):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path / "data"))
    runner, url = await _fake_relay(None, status=status)
    remote.save_config({"relay": url, "device_id": "d", "device_token": "t", "name": "x"})
    session = make_session(tmp_path)
    note = await asyncio.get_running_loop().run_in_executor(
        None, lambda: remote_control.start(session, driver=IdleDriver(), say=lambda m: None))
    await runner.cleanup()
    return note, remote_control.active(session)


def test_the_session_limit_is_reported_and_nothing_stays_on(tmp_path, monkeypatch):
    note, on = asyncio.run(_refused(tmp_path, monkeypatch, 409))
    assert "limit" in note and "couldn't start" in note and not on


def test_two_chats_on_one_machine_can_both_have_remote_control(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setenv("HEXIEL_DATA", str(tmp_path / "data"))
        sids = []

        async def relay(request, ws):
            sids.append(request.query["session"])
            async for _ in ws:
                pass
        runner, url = await _fake_relay(relay)
        remote.save_config({"relay": url, "device_id": "d", "device_token": "t", "name": "x"})
        a, b = make_session(tmp_path), make_session(tmp_path)
        loop = asyncio.get_running_loop()
        notes = [await loop.run_in_executor(None, lambda s=s: remote_control.start(s, driver=IdleDriver())) for s in (a, b)]
        both = remote_control.active(a) and remote_control.active(b)
        for s in (a, b):
            await loop.run_in_executor(None, lambda s=s: remote_control.stop(s))
        await runner.cleanup()
        return notes, both, sids
    notes, both, sids = asyncio.run(go())
    assert all(n.startswith("Remote control ON") for n in notes) and both
    assert len(set(sids)) == 2


def test_the_background_service_runs_jobs_but_shows_no_chat():
    async def go():
        sent = []

        class WS:
            async def send_json(self, frame): sent.append(frame)
        tunnel = remote.Tunnel({"relay": "http://r"}, "", "/w", None, kind="runner")
        await tunnel._handle(WS(), None, {"t": "http", "id": "1", "method": "GET", "path": "/"})
        await tunnel._handle(WS(), None, {"t": "ws_open", "sid": "s"})
        return sent
    sent = asyncio.run(go())
    assert sent[0]["t"] == "http_res" and sent[0]["status"] == 404
    assert sent[1] == {"t": "ws_close", "sid": "s"}


def test_status_frames_say_when_a_turn_is_running(tmp_path):
    """The Stop button follows status.busy: it used to read an attribute nothing set, so a phone that
    reconnected mid-turn only ever showed Send and could not stop the turn."""
    from hexiel.webui import WebState, status_state
    session = make_session(tmp_path)
    driver = IdleDriver()
    state = WebState(session, driver)
    assert status_state(session)["busy"] is False
    driver.working = True
    assert status_state(session)["busy"] is True and state.catch_up()["status"]["busy"] is True
    session.remove_mirror(state)


def test_services_installed_before_sessions_become_the_automation_runner(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path))
    work = remote.workspace_dir()
    assert remote.is_service_invocation(str(work), remote.SERVICE_PORT)
    assert not remote.is_service_invocation(str(work), 8777)              # someone's own web console there
    assert not remote.is_service_invocation(str(tmp_path), remote.SERVICE_PORT)


def test_the_session_list_reads_well():
    text = remote.format_sessions({"max_sessions": 3, "sessions": [
        {"kind": "chat", "title": "fix it", "ui": "terminal", "cwd": "/p", "busy": True},
        {"kind": "legacy", "title": ""}]})
    assert "(1; at most 3 per account)" in text and "fix it — terminal, /p (working)" in text
    assert "none" in remote.format_sessions({"sessions": []})


def test_a_compacted_chat_keeps_a_real_title(tmp_path):
    session = make_session(tmp_path)
    session.messages = [{"role": "user", "content": "<auto-compaction>\n\nsummary\n</auto-compaction>"},
                        {"role": "user", "content": "now deploy it"}]
    assert remote.chat_title(session) == "now deploy it"


def test_an_unanswered_approval_is_pushed_to_the_phone_and_an_answered_one_is_not(tmp_path, monkeypatch):
    session = make_session(tmp_path)
    monkeypatch.setattr(type(session), "APPROVAL_REMINDER", 0.05)
    pushed = []
    session.remote_notify = lambda kind, text: pushed.append((kind, text))
    session.on_approve = lambda *a: "yes"                                   # answered at once
    assert session.on_approve("bash", {"command": "ls"}) == "yes"
    time.sleep(0.15)
    assert pushed == []
    session.on_approve = lambda *a: (time.sleep(0.3), "no")[1]             # nobody at the desk
    assert session.on_approve("bash", {"command": "rm -rf   build"}) == "no"
    assert pushed == [("approval", "bash: rm -rf build")]
    session.remote_notify = None
    assert session.on_approve is not None and session.on_approve("x", {}) == "no"


def test_the_assistant_tunnel_shows_its_chat_but_is_never_ended_or_given_jobs(tmp_path):
    async def go():
        sent = []

        class WS:
            async def send_json(self, frame): sent.append(frame)
        session = make_session(tmp_path)
        tunnel = remote.Tunnel({"relay": "http://r"}, "http://127.0.0.1:9", "/w", session, kind="assistant")
        ended = []
        tunnel.on_end = lambda: ended.append(True)
        await tunnel._handle(WS(), None, {"t": "end"})
        await tunnel._handle(WS(), None, {"t": "run", "id": "1", "prompt": "p"})
        return tunnel, ended, sent, session
    tunnel, ended, sent, session = asyncio.run(go())
    assert not tunnel.ended and ended == [] and sent == []
    assert tunnel._status() == {"title": "Assistant", "busy": False}
    assert session.remote_notify == tunnel.notify
    assert remote.assistant_sid("dev1") == remote.assistant_sid("dev1") != remote.assistant_sid("dev2")


def test_the_assistant_chat_is_resumed_with_its_phone_instructions(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path / "data"))
    remote.save_config({"relay": "http://r", "device_id": "d", "device_token": "t"})
    work = tmp_path / "w"
    work.mkdir()
    first = remote.load_assistant(str(work))
    assert remote.ASSISTANT_NOTE in first.system_prompt
    assert remote.load_config()["assistant_session"] == first.id
    first.messages = [{"role": "user", "content": "what's on my calendar"}]
    first.save()
    again = remote.load_assistant(str(work))
    assert again.id == first.id and again.messages == first.messages
