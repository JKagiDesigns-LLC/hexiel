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
import stat
import sys
from pathlib import Path

from .config import home_data_dir

DEFAULT_RELAY = "https://remote.hexiel.tech"
ALLOWED_PREFIXES = ("/static/", "/api/")
ALLOWED_EXACT = ("/", "/ws")
MAX_BODY = 8 * 1024 * 1024
BACKOFF_CAP = 60
RUN_TIMEOUT = 30 * 60


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


class Tunnel:
    """One live connection to the relay; serves tunneled requests against the local web UI."""

    def __init__(self, cfg: dict, local_base: str, cwd: str, session) -> None:
        self.cfg, self.local, self.cwd, self.session = cfg, local_base.rstrip("/"), cwd, session
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
                print(f"  remote: {note.get(exc.status, f'relay refused ({exc.status})')}", file=sys.stderr)
                if exc.status in (401, 402):
                    delay = BACKOFF_CAP
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
                print(f"  remote: disconnected ({type(exc).__name__}); retrying in {delay}s", file=sys.stderr)
            try:
                await asyncio.wait_for(self.stop.wait(), delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, BACKOFF_CAP)

    async def _connect_once(self, aiohttp) -> None:
        headers = {"Authorization": f"Bearer {self.cfg['device_token']}"}
        # The relay connection honours HTTP(S)_PROXY / NO_PROXY; the session for the local web UI must not.
        async with aiohttp.ClientSession() as http, aiohttp.ClientSession(trust_env=True) as relay_http:
            async with relay_http.ws_connect(_relay_ws_url(self.cfg["relay"]), headers=headers,
                                       heartbeat=25, max_msg_size=32 * 1024 * 1024) as ws:
                await ws.send_json({"t": "hello", "name": self.cfg.get("name", ""), "cwd": self.cwd})
                print("  remote: connected — open https://remote.hexiel.tech to drive this machine")
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        asyncio.create_task(self._handle(ws, http, json.loads(msg.data)))
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
               "model": ""}
        loop = asyncio.get_running_loop()
        try:
            ok, answer = await asyncio.wait_for(loop.run_in_executor(None, jobs.run_job, job), RUN_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - report failure to the relay instead of hanging
            ok, answer = False, f"run failed: {type(exc).__name__}: {exc}"
        jobs.deliver(job, ok, answer)
        await ws.send_json({"t": "run_result", "id": f["id"], "ok": ok, "answer": answer[:20000]})


async def serve_with_remote(session, host: str, port: int) -> None:
    """Run the local web UI and the tunnel together (hexiel --serve --remote)."""
    from aiohttp import web
    from .webui import make_app
    cfg = load_config()
    if not cfg.get("device_token"):
        raise SystemExit("not signed in to Hexiel Remote — run:  hexiel remote login")
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
    """hexiel remote login [--relay URL] | logout | status"""
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
        print(f"signed in — this machine is device {device}. Start it with:  hexiel --serve --remote")
        return 0
    if cmd == "logout":
        config_file().unlink(missing_ok=True)
        print("signed out (revoke the device on the account page too if it was lost)")
        return 0
    state = f"signed in as device {cfg['device_id']} → {cfg['relay']}" if cfg.get("device_token") else "not signed in"
    print(f"hexiel remote: {state}")
    return 0
