"""/remote-control: from inside a running chat (terminal or web UI), make that same chat drivable from
remote.hexiel.tech. The chat process itself holds the relay connection, so the browser sees the live
conversation — the same history, tool output and approvals — and the terminal sees what the browser types.

Each chat with remote control on is its own session on the account page (the relay allows a few per account).
Ending the chat ends its session; ending the session from the account page turns remote control off here and
leaves the chat open. The background service is separate: it only runs automations."""

import asyncio
import atexit
import platform
import threading

from . import remote

_active: dict[int, "Control"] = {}                 # id(session) -> its live remote control


def signed_in() -> bool:
    return bool(remote.load_config().get("device_token"))


def sign_in(email: str, password: str) -> str:
    """Exchange the remote.hexiel.tech login for a device token on this machine."""
    if not email or not password:
        raise RuntimeError("enter your email and password")
    cfg = remote.load_config()
    return remote.login(cfg.get("relay") or remote.DEFAULT_RELAY, email, password, platform.node() or "this machine")


def active(session) -> bool:
    return id(session) in _active


class Control:
    def __init__(self, session, local_base: str = "", driver=None, say=None) -> None:
        self.session, self.driver, self.local_base = session, driver, local_base
        self.say = say or (lambda text: None)
        self.tunnel: remote.Tunnel | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.ready = threading.Event()
        self.error = ""
        self.state = None
        self.stopped = False

    def start(self) -> None:
        cfg = remote.load_config()
        self.thread = threading.Thread(target=self._run, args=(cfg,), daemon=True, name="hexiel-remote-control")
        self.thread.start()
        if not self.ready.wait(15) or self.error:
            err = self.error or "timed out starting the connection"
            self.stop()
            raise RuntimeError(err)
        tunnel = self.tunnel
        if not tunnel.attempted.wait(20):
            self.stop()
            raise RuntimeError("timed out connecting to the relay")
        if not tunnel.connected:
            problem = tunnel.problem or "the relay did not accept the connection"
            self.stop()
            raise RuntimeError(problem)

    def _run(self, cfg: dict) -> None:
        try:
            asyncio.run(self._main(cfg))
        except Exception as exc:  # noqa: BLE001 - reported to the user, never raised into the chat
            self.error = self.error or f"{type(exc).__name__}: {exc}"
            self.ready.set()

    async def _main(self, cfg: dict) -> None:
        from aiohttp import web
        self.loop = asyncio.get_running_loop()
        runner = None
        base = self.local_base
        if not base:                                          # a terminal chat has no web server yet: serve this session
            from .webui import make_app
            app = make_app(self.session, self.driver)
            self.state = app["state"]
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self.tunnel = remote.Tunnel(cfg, base, self.session.cwd, self.session, say=self.say,
                                    ui="web" if self.local_base else "terminal", on_end=self._ended_remotely)
        self.ready.set()
        try:
            await self.tunnel.run_forever()
        finally:
            if runner is not None:
                await runner.cleanup()

    def _ended_remotely(self) -> None:
        if _active.get(id(self.session)) is self:
            self.say("remote control was turned off from remote.hexiel.tech — this chat is local again")
            _active.pop(id(self.session), None)
            self.stop()

    def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        atexit.unregister(self.stop)
        if self.state is not None:
            self.session.remove_mirror(self.state)
        if self.tunnel is not None and self.session.remote_notify == self.tunnel.notify:
            self.session.remote_notify = None
        if self.tunnel is not None and self.loop is not None and not self.loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(self.tunnel.close(), self.loop)
            except RuntimeError:                              # the loop finished meanwhile
                pass
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=6)


def start(session, local_base: str = "", driver=None, say=None) -> str:
    """Turn remote control on for this chat. Returns the line to show; the caller checks signed_in() first."""
    if active(session):
        return status_text(session)
    control = Control(session, local_base, driver, say)
    try:
        control.start()
    except RuntimeError as exc:
        return f"couldn't start remote control: {exc}"
    _active[id(session)] = control
    atexit.register(control.stop)
    return ("Remote control ON — open https://remote.hexiel.tech and pick this chat to drive it from the browser "
            "or your phone. `/remote-control off` (or leaving this chat) ends it.")


def stop(session) -> str:
    control = _active.pop(id(session), None)
    if control is None:
        return "remote control isn't on for this chat"
    control.stop()
    return "Remote control OFF — this chat is local again"


def status_text(session) -> str:
    control = _active.get(id(session))
    if control is None:
        return "remote control is off for this chat — /remote-control turns it on"
    t = control.tunnel
    if t is not None and t.connected:
        return "Remote control ON — connected. Open https://remote.hexiel.tech and pick this chat to drive it."
    return "Remote control ON — reconnecting…" + (f" ({t.problem})" if t is not None and t.problem else "")
