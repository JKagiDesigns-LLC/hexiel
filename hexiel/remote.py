"""Hexiel Remote (paid add-on): drive this machine's hexiel from a browser or phone, and let
cloud triggers (schedules, webhooks) start runs here.

The client is open source; the subscription is enforced by the relay (remote.hexiel.tech).
This machine dials OUT to the relay over a websocket — nothing is exposed locally. The relay
tunnels HTTP and websocket traffic from the signed-in browser to the local web UI, and
dispatches automation runs. Everything the web UI can do (including shell commands, behind
its normal approval prompts) is reachable by whoever is signed in to your account, so the
relay requires a valid subscription AND your login on every connection."""

import asyncio
import base64
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from .config import home_data_dir

DEFAULT_RELAY = "https://remote.hexiel.tech"
ALLOWED_PREFIXES = ("/static/", "/api/")
ALLOWED_EXACT = ("/", "/ws")
MAX_BODY = 8 * 1024 * 1024
BACKOFF_CAP = 60
RUN_TIMEOUT = 30 * 60
SERVICE_PORT = 8778           # the background service's local web UI (8777 stays free for `hexiel --serve`)
SERVICE_NAME = "hexiel-remote"


def config_file() -> Path:
    return home_data_dir() / "remote.json"


def load_config() -> dict:
    try:
        return json.loads(config_file().read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2))
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)        # holds the device token


def path_allowed(path: str) -> bool:
    """Only the web UI's own pages and API cross the tunnel — never the /v1 model API."""
    clean = path.split("?", 1)[0]
    if clean.startswith("/api/remote/"):                 # sign-in for /remote-control: local browser only
        return False
    return clean in ALLOWED_EXACT or (clean.startswith(ALLOWED_PREFIXES) and ".." not in clean)


def _relay_ws_url(relay: str) -> str:
    return relay.replace("https://", "wss://", 1).replace("http://", "ws://", 1).rstrip("/") + "/ws/device"


def login(relay: str, email: str, password: str, name: str) -> str:
    """Exchange account credentials for a device token (stored, revocable on the account page)."""
    import httpx
    resp = httpx.post(relay.rstrip("/") + "/api/device/register", timeout=20,
                      json={"email": email, "password": password, "name": name})
    if resp.status_code != 200:
        try:
            detail = resp.json().get("error", resp.text)
        except ValueError:
            detail = resp.text
        raise RuntimeError(f"login failed: {detail}")
    data = resp.json()
    save_config({"relay": relay.rstrip("/"), "device_id": data["device_id"],
                 "device_token": data["token"], "name": name})
    return data["device_id"]


def model_profiles() -> dict:
    """The model profiles this machine can run, for the automation form's dropdown. Names and which key a
    profile needs — never a key. `needs_key` means that key is missing here, so an unattended run on that
    profile would fail."""
    try:
        from .config import load_config as load_hexiel_config
        cfg = load_hexiel_config()
    except (Exception, SystemExit):
        return {"default": "", "profiles": []}
    return {"default": cfg.default_model,
            "profiles": [{"name": name, "model": m.model, "provider": m.provider, "key_env": m.api_key_env,
                          "needs_key": bool(m.api_key_env) and not os.environ.get(m.api_key_env)}
                         for name, m in cfg.models.items()]}


class Tunnel:
    """One live connection to the relay; serves tunneled requests against the local web UI."""

    def __init__(self, cfg: dict, local_base: str, cwd: str, session, say=None) -> None:
        self.cfg, self.local, self.cwd, self.session = cfg, local_base.rstrip("/"), cwd, session
        self.say = say or (lambda text: print(f"  remote: {text}", file=sys.stderr))
        self.connected = False
        self.problem = ""
        self._ws = None
        self.local_ws: dict[str, object] = {}
        self.stop = asyncio.Event()

    async def run_forever(self) -> None:
        import aiohttp
        delay = 1
        while not self.stop.is_set():
            try:
                await self._connect_once(aiohttp)
                delay = 1
            except aiohttp.WSServerHandshakeError as exc:
                note = {401: "device token rejected — run `hexiel remote login`",
                        402: "no active Hexiel Remote subscription — see https://remote.hexiel.tech"}
                self.problem = note.get(exc.status, f"relay refused ({exc.status})")
                self.say(self.problem)
                if exc.status in (401, 402):
                    delay = BACKOFF_CAP
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
                self.say(f"disconnected ({type(exc).__name__}); retrying in {delay}s")
            try:
                await asyncio.wait_for(self.stop.wait(), delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, BACKOFF_CAP)

    async def close(self) -> None:
        """Stop for good: leave the relay now rather than at the next reconnect."""
        self.stop.set()
        if self._ws is not None:
            await self._ws.close()

    async def _connect_once(self, aiohttp) -> None:
        headers = {"Authorization": f"Bearer {self.cfg['device_token']}"}
        # The relay connection honours HTTP(S)_PROXY / NO_PROXY; the session for the local web UI must not.
        async with aiohttp.ClientSession() as http, aiohttp.ClientSession(trust_env=True) as relay_http:
            async with relay_http.ws_connect(_relay_ws_url(self.cfg["relay"]), headers=headers,
                                       heartbeat=25, max_msg_size=32 * 1024 * 1024) as ws:
                await ws.send_json({"t": "hello", "name": self.cfg.get("name", ""), "cwd": self.cwd, "models": model_profiles()})
                self._ws = ws
                self.connected, self.problem = True, ""
                self.say("connected — open https://remote.hexiel.tech to drive this machine")
                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            asyncio.create_task(self._handle(ws, http, json.loads(msg.data)))
                finally:
                    self.connected = False
            for sid in list(self.local_ws):
                await self._close_local(sid)

    async def _handle(self, ws, http, frame: dict) -> None:
        kind = frame.get("t")
        try:
            if kind == "http":
                await self._http(ws, http, frame)
            elif kind == "ws_open":
                await self._ws_open(ws, http, frame)
            elif kind == "ws_msg":
                local = await self._await_local(frame["sid"])
                if local is not None:
                    await local.send_str(frame["data"])
            elif kind == "ws_close":
                await self._close_local(frame["sid"])
            elif kind == "run":
                await self._run(ws, frame)
        except Exception as exc:  # noqa: BLE001 - one bad frame must not drop the tunnel
            if kind == "http":
                await ws.send_json({"t": "http_res", "id": frame.get("id"), "status": 502,
                                    "headers": {}, "body": base64.b64encode(str(exc).encode()).decode()})

    async def _http(self, ws, http, f: dict) -> None:
        path = f["path"]
        if not path_allowed(path):
            return await self._reply(ws, f["id"], 404, {}, b"not found")
        body = base64.b64decode(f.get("body", ""))
        keep = {k: v for k, v in f.get("headers", {}).items() if k.lower() in ("content-type", "accept")}
        async with http.request(f["method"], self.local + path, data=body or None, headers=keep) as r:
            data = await r.content.read(MAX_BODY + 1)
            if len(data) > MAX_BODY:
                return await self._reply(ws, f["id"], 502, {}, b"response too large")
            ctype = r.headers.get("content-type", "")
            await self._reply(ws, f["id"], r.status, {"content-type": ctype}, data)

    @staticmethod
    async def _reply(ws, rid, status: int, headers: dict, data: bytes) -> None:
        await ws.send_json({"t": "http_res", "id": rid, "status": status, "headers": headers,
                            "body": base64.b64encode(data).decode()})

    async def _ws_open(self, ws, http, f: dict) -> None:
        sid = f["sid"]
        local = await http.ws_connect(self.local.replace("http", "ws", 1) + "/ws",
                                      max_msg_size=32 * 1024 * 1024)
        self.local_ws[sid] = local
        async for msg in local:
            if msg.type.name == "TEXT":
                await ws.send_json({"t": "ws_msg", "sid": sid, "data": msg.data})
        self.local_ws.pop(sid, None)
        await ws.send_json({"t": "ws_close", "sid": sid})

    async def _await_local(self, sid: str):
        for _ in range(50):                      # the browser may speak before the local socket is up
            local = self.local_ws.get(sid)
            if local is not None:
                return local
            await asyncio.sleep(0.1)
        return None

    async def _close_local(self, sid: str) -> None:
        local = self.local_ws.pop(sid, None)
        if local is not None:
            await local.close()

    async def _run(self, ws, f: dict) -> None:
        """An automation fired: run it headless (read-only unless the job says otherwise)."""
        from . import jobs
        job = {"name": f.get("name", "automation"), "prompt": f["prompt"], "cwd": self.cwd,
               "access": "full" if f.get("access") == "full" else "readonly",
               "model": str(f.get("model") or "")}
        loop = asyncio.get_running_loop()

        def progress(text: str) -> None:        # called from the job's thread; the page shows it live
            sent = asyncio.run_coroutine_threadsafe(ws.send_json({"t": "run_progress", "id": f["id"], "text": text}), loop)
            sent.add_done_callback(lambda fut: fut.exception())     # a dropped connection is not the job's problem
        try:
            ok, answer = await asyncio.wait_for(loop.run_in_executor(None, lambda: jobs.run_job(job, progress)), RUN_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - report failure to the relay instead of hanging
            ok, answer = False, f"run failed: {type(exc).__name__}: {exc}"
        jobs.deliver(job, ok, answer)
        await ws.send_json({"t": "run_result", "id": f["id"], "ok": ok, "answer": answer[:20000]})


_lock_handle = None


def acquire_single_instance() -> bool:
    """One connection per machine: the relay keeps only the newest connection for a device, so two
    hexiel processes dialing in with the same token would knock each other off forever."""
    global _lock_handle
    path = home_data_dir() / "remote.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)                        # msvcrt locks from the current position
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _lock_handle = handle                         # keep open: the lock lives as long as this process
    return True


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def log_file() -> Path:
    return home_data_dir() / "remote.log"


def ensure_streams() -> None:
    """Started without a console (pythonw, a scheduled task): there is no stdout/stderr, so anything
    that prints would crash. Send it to the log file instead."""
    if sys.stderr is None or sys.stdout is None:
        log_file().parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(log_file(), "a", buffering=1, encoding="utf-8")


def workspace_dir() -> Path:
    """Where the background service (and so every automation) works. A dedicated folder, not the home
    directory: macOS parks a background process that touches Documents/Desktop behind a privacy prompt
    nobody sees, and a read-only automation has no business scanning everything you own."""
    chosen = load_config().get("workdir")
    path = Path(chosen) if chosen else home_data_dir() / "remote-workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


SERVICE_ENV_EXTRA = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")


def service_env_file() -> Path:
    return home_data_dir() / "service.env"


def write_service_env() -> tuple[list[str], list[str]]:
    """A service started by systemd/launchd/Task Scheduler never sees the keys you export in your shell
    profile, so unattended runs could not use a cloud model. Record the ones your configured models need
    (and any proxy settings) in a private file hexiel loads at startup. Returns (captured, missing) names."""
    try:
        from .config import load_config
        profiles = load_config().models.values()
    except (Exception, SystemExit):
        profiles = []
    wanted = list(dict.fromkeys(p.api_key_env for p in profiles if getattr(p, "api_key_env", "")))
    captured = [name for name in wanted if os.environ.get(name)]
    missing = [name for name in wanted if not os.environ.get(name)]
    lines = [f"{name}={os.environ[name]}" for name in [*captured, *(n for n in SERVICE_ENV_EXTRA if os.environ.get(n))]]
    path = service_env_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""))
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)        # holds API keys
    return captured, missing


def load_service_env() -> None:
    """Add the recorded keys to this process — never overriding anything already set."""
    try:
        for line in service_env_file().read_text().splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() and not name.startswith("#"):
                os.environ.setdefault(name.strip(), value)
    except OSError:
        pass


def _service_command() -> list[str]:
    exe = sys.executable
    if sys.platform == "win32":                    # no console window at every login
        windowless = Path(exe).with_name("pythonw.exe")
        exe = str(windowless) if windowless.exists() else exe
    return [exe, "-m", "hexiel", "--serve", "--remote", "--port", str(SERVICE_PORT), str(workspace_dir())]


def systemd_unit() -> str:
    exec_start = " ".join(f'"{a}"' if " " in a else a for a in _service_command())
    return ("[Unit]\nDescription=Hexiel Remote: keeps this machine connected to remote.hexiel.tech\n"
            "After=network-online.target\nWants=network-online.target\n\n"
            f"[Service]\nExecStart={exec_start}\nWorkingDirectory={workspace_dir()}\nRestart=always\nRestartSec=10\n\n"
            "[Install]\nWantedBy=default.target\n")


def launchd_plist() -> str:
    args = "".join(f"<string>{a}</string>" for a in _service_command())
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
            "<key>Label</key><string>tech.hexiel.remote</string>"
            f"<key>ProgramArguments</key><array>{args}</array>"
            f"<key>WorkingDirectory</key><string>{workspace_dir()}</string>"
            f"<key>StandardOutPath</key><string>{log_file()}</string>"
            f"<key>StandardErrorPath</key><string>{log_file()}</string>"
            "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/></dict></plist>\n")


def _unit_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / "tech.hexiel.remote.plist"
    return Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service"


def _env_note() -> str:
    captured, missing = write_service_env()
    note = f"\n  gave the service these keys from your environment: {', '.join(captured)}" if captured else ""
    if missing:
        note += (f"\n  ! not set in this shell, so unattended runs can't use those models: {', '.join(missing)} "
                 "— export them and run `hexiel remote install` again")
    return note


def install_service(workdir: str | None = None) -> str:
    """Keep this machine connected whenever it is on and you are logged in. Returns a status line."""
    cfg = load_config()
    if not cfg.get("device_token"):
        raise RuntimeError("not signed in — run `hexiel remote login` first")
    if workdir:
        folder = Path(workdir).expanduser().resolve()
        if not folder.is_dir():
            raise RuntimeError(f"{folder} is not a folder")
        save_config({**cfg, "workdir": str(folder)})
    if sys.platform == "win32":
        note = _env_note()
        cmd = subprocess.list2cmdline(_service_command())
        done = _run(["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", "HexielRemote", "/TR", cmd])
        if done.returncode:
            raise RuntimeError(f"could not create the scheduled task: {done.stderr.strip() or done.stdout.strip()}")
        _run(["schtasks", "/Run", "/TN", "HexielRemote"])
        return "installed the HexielRemote scheduled task (starts at login) and started it" + note
    note = _env_note()
    path = _unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        log_file().parent.mkdir(parents=True, exist_ok=True)
        path.write_text(launchd_plist())
        domain = f"gui/{os.getuid()}"
        _run(["launchctl", "bootout", domain, str(path)])             # replace an older copy, if any
        done = _run(["launchctl", "bootstrap", domain, str(path)])
        if done.returncode:                                           # older macOS: the legacy verb
            done = _run(["launchctl", "load", "-w", str(path)])
        if done.returncode:
            raise RuntimeError(f"launchctl failed: {done.stderr.strip()}")
        return f"installed {path} and started it (starts at login)" + note
    if not shutil.which("systemctl"):
        raise RuntimeError("no systemd found — keep `hexiel --serve --remote` running with your init system")
    path.write_text(systemd_unit())
    for step in (["daemon-reload"], ["enable", "--now", SERVICE_NAME]):
        done = _run(["systemctl", "--user", *step])
        if done.returncode:
            raise RuntimeError(f"systemctl {step[0]} failed: {done.stderr.strip()}")
    return (f"installed {path} and started it (starts at login). To also run while nobody is logged in: "
            "loginctl enable-linger $USER" + note)


def uninstall_service() -> str:
    service_env_file().unlink(missing_ok=True)             # it holds API keys
    if sys.platform == "win32":
        _run(["schtasks", "/Delete", "/F", "/TN", "HexielRemote"])
        return "removed the HexielRemote scheduled task"
    path = _unit_path()
    if sys.platform == "darwin":
        if _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)]).returncode:
            _run(["launchctl", "unload", str(path)])
    elif shutil.which("systemctl"):
        _run(["systemctl", "--user", "disable", "--now", SERVICE_NAME])
    path.unlink(missing_ok=True)
    return "removed the background service"


def service_active() -> bool:
    try:
        if sys.platform == "win32":
            return _run(["schtasks", "/Query", "/TN", "HexielRemote"]).returncode == 0
        if sys.platform == "darwin":
            return _run(["launchctl", "list", "tech.hexiel.remote"]).returncode == 0
        return shutil.which("systemctl") is not None and \
            _run(["systemctl", "--user", "is-active", SERVICE_NAME]).stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def _service_names() -> tuple[list[str], list[str]]:
    """(stop command, start command) for the background service on this OS."""
    if sys.platform == "win32":
        return ["schtasks", "/End", "/TN", "HexielRemote"], ["schtasks", "/Run", "/TN", "HexielRemote"]
    if sys.platform == "darwin":
        return (["launchctl", "bootout", f"gui/{os.getuid()}", str(_unit_path())],
                ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(_unit_path())])
    return ["systemctl", "--user", "stop", SERVICE_NAME], ["systemctl", "--user", "start", SERVICE_NAME]


def pause_file() -> Path:
    return home_data_dir() / "remote.paused"


def pause_service() -> bool:
    """Stop the background service so a live chat can hold the one relay connection. Remembered on disk so
    the service is brought back even if that chat is killed. Returns whether it was running."""
    if not service_active():
        return False
    pause_file().write_text(str(os.getpid()))
    _run(_service_names()[0])
    return True


def resume_service() -> None:
    marker = pause_file()
    if not marker.exists():
        return
    marker.unlink(missing_ok=True)
    start = _service_names()[1]
    if _run(start).returncode and sys.platform == "darwin":
        _run(["launchctl", "load", "-w", str(_unit_path())])


def resume_if_orphaned() -> None:
    """A chat that paused the service was killed without cleaning up: bring the service back."""
    try:
        pid = int(pause_file().read_text().strip())
    except (OSError, ValueError):
        return
    try:
        os.kill(pid, 0)
        if pid != os.getpid():
            return                                            # that chat is still alive and owns the connection
    except PermissionError:
        return
    except OSError:
        pass
    resume_service()


def release_single_instance() -> None:
    global _lock_handle
    if _lock_handle is not None:
        try:
            _lock_handle.close()
        finally:
            _lock_handle = None


def relay_says_online(cfg: dict) -> bool | None:
    """Ask the relay whether it sees this machine; None if it could not be asked."""
    import httpx
    try:
        resp = httpx.get(cfg["relay"] + "/api/device/status", timeout=10,
                         headers={"Authorization": f"Bearer {cfg['device_token']}"})
        return bool(resp.json().get("online")) if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError, KeyError):
        return None


async def serve_with_remote(session, host: str, port: int) -> None:
    """Run the local web UI and the tunnel together (hexiel --serve --remote)."""
    from aiohttp import web
    from .webui import make_app
    cfg = load_config()
    if not cfg.get("device_token"):
        raise SystemExit("not signed in to Hexiel Remote — run:  hexiel remote login")
    if not acquire_single_instance():
        raise SystemExit("hexiel remote is already connected from this machine (the background service, or another "
                         "`hexiel --serve --remote`). Check it with `hexiel remote status`.")
    runner = web.AppRunner(make_app(session))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    tunnel = Tunnel(cfg, f"http://127.0.0.1:{port}", session.cwd, session)
    try:
        await tunnel.run_forever()
    finally:
        tunnel.stop.set()
        await runner.cleanup()


def remote_cli(argv: list[str]) -> int:
    """hexiel remote login [--relay URL] | logout | status | install [--dir FOLDER] | uninstall"""
    import getpass
    import platform
    cmd = argv[0] if argv else "status"
    cfg = load_config()
    if cmd == "login":
        relay = os.environ.get("HEXIEL_RELAY", cfg.get("relay", DEFAULT_RELAY))
        email = input("Hexiel Remote email: ").strip()
        password = getpass.getpass("Password: ")
        try:
            device = login(relay, email, password, platform.node() or "this machine")
        except (RuntimeError, OSError) as exc:
            print(exc)
            return 1
        print(f"signed in — this machine is device {device}.")
        # Being signed in is not being connected: something has to keep the connection open.
        keep = "n"
        if sys.stdin.isatty():
            keep = input("Keep it connected in the background whenever this computer is on, so automations "
                         "can run? [Y/n] ").strip().lower() or "y"
        if keep.startswith("y"):
            try:
                print(install_service())
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                print(f"could not set that up: {exc}\nRun it yourself with:  hexiel --serve --remote")
                return 1
        else:
            print("Connect it with:  hexiel --serve --remote   (or later: hexiel remote install)")
        return 0
    if cmd == "install":
        workdir = argv[argv.index("--dir") + 1] if "--dir" in argv and argv.index("--dir") + 1 < len(argv) else None
        try:
            print(install_service(workdir))
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            print(exc)
            return 1
        return 0
    if cmd == "uninstall":
        print(uninstall_service())
        return 0
    if cmd == "logout":
        try:
            uninstall_service()
        except (OSError, subprocess.SubprocessError):
            pass
        config_file().unlink(missing_ok=True)
        print("signed out and stopped the background service (revoke the device on the account page too if it was lost)")
        return 0
    if not cfg.get("device_token"):
        print("hexiel remote: not signed in — run `hexiel remote login`")
        return 0
    online = relay_says_online(cfg)
    print(f"hexiel remote: signed in as device {cfg['device_id']} → {cfg['relay']}")
    print("  background service: " + ("running" if service_active() else "NOT running — `hexiel remote install`"))
    print("  relay sees this machine: " + {True: "connected", False: "NOT connected (automations will queue)",
                                          None: "unknown (no answer to the status check — the relay may not support it yet)"}[online])
    return 0
