"""/remote-control: from inside a running chat (terminal or web UI), make that same chat drivable from
remote.hexiel.tech. The chat process itself holds the relay connection, so the browser sees the live
conversation — the same history, tool output and approvals — and the terminal sees what the browser types.

The relay accepts one connection per machine, so the background service (if installed) is paused while a
chat has remote control on, and started again when it is turned off or the chat exits."""

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
        self.paused_service = False
        self.tunnel: remote.Tunnel | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.ready = threading.Event()
        self.error = ""
        self.state = None
        self.stopped = False

    def start(self) -> None:
        cfg = remote.load_config()
        self.paused_service = remote.pause_service()
        if not self._lock():
            self._undo()
            raise RuntimeError("another hexiel on this machine already holds the remote connection "
                               "(`/remote-control off` there, or `hexiel remote status`)")
        self.thread = threading.Thread(target=self._run, args=(cfg,), daemon=True, name="hexiel-remote-control")
        self.thread.start()
        if not self.ready.wait(15) or self.error:
            err = self.error or "timed out starting the connection"
            self.stop()
            raise RuntimeError(err)

    def _lock(self) -> bool:
        import time
        for _ in range(20):                                   # the service needs a moment to let go
            if remote.acquire_single_instance():
                return True
            time.sleep(0.25)
        return False

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
        self.tunnel = remote.Tunnel(cfg, base, self.session.cwd, self.session, say=self.say)
        self.ready.set()
        try:
            await self.tunnel.run_forever()
        finally:
            if runner is not None:
                await runner.cleanup()

    def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        atexit.unregister(self.stop)
        if self.state is not None:
            self.session.remove_mirror(self.state)
        if self.tunnel is not None and self.loop is not None:
            asyncio.run_coroutine_threadsafe(self.tunnel.close(), self.loop)
        if self.thread is not None:
            self.thread.join(timeout=6)
        self._undo()

    def _undo(self) -> None:
        remote.release_single_instance()
        if self.paused_service:
            remote.resume_service()
            self.paused_service = False


def start(session, local_base: str = "", driver=None, say=None) -> str:
    """Turn remote control on for this chat. Returns the line to show; the caller checks signed_in() first."""
    if active(session):
        return status_text(session)
    if remote._lock_handle is not None:
        return "this chat is already connected to Hexiel Remote"       # e.g. typed in the browser through the relay
    control = Control(session, local_base, driver, say)
    try:
        control.start()
    except RuntimeError as exc:
        return f"couldn't start remote control: {exc}"
    _active[id(session)] = control
    atexit.register(control.stop)
    back = " The background service is paused meanwhile and comes back when you turn this off or exit." \
        if control.paused_service else ""
    return ("Remote control ON — open https://remote.hexiel.tech and pick this machine to drive this same chat "
            "from the browser or your phone. `/remote-control off` ends it." + back)


def stop(session) -> str:
    control = _active.pop(id(session), None)
    if control is None:
        return "remote control isn't on for this chat"
    resumed = control.paused_service
    control.stop()
    return "Remote control OFF — this chat is local again" + (" (background service restarted)" if resumed else "")


def status_text(session) -> str:
    control = _active.get(id(session))
    if control is None:
        return "remote control is off for this chat — /remote-control turns it on"
    t = control.tunnel
    if t is not None and t.connected:
        return "Remote control ON — connected. Open https://remote.hexiel.tech to drive this chat."
    return "Remote control ON — connecting…" + (f" ({t.problem})" if t is not None and t.problem else "")
