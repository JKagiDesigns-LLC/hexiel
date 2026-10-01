"""MCP (Model Context Protocol) client: run MCP servers' tools inside hexiel.

Servers come from (later wins on name clashes):
  * ~/.config/hexiel/mcp.json and <project>/.mcp.json — Claude Code's format:
      {"mcpServers": {"name": {"command": "npx", "args": [...], "env": {...}},
                      "remote": {"type": "http", "url": "https://…", "headers": {...}}}}
  * [mcp.servers.<name>] tables in config.toml (same keys, plus `tools = [...]`
    to expose only some of a server's tools and `enabled = false`)

Transports: stdio (newline-delimited JSON-RPC over a subprocess) and streamable
HTTP (JSON or SSE responses, Mcp-Session-Id). Each tool becomes a hexiel tool
named mcp__<server>__<tool>; tools a server marks readOnlyHint skip approval.
"""

import atexit
import base64
import itertools
import json
import os
import queue
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import __version__

PROTOCOL_VERSION = "2025-06-18"
START_TIMEOUT = 20.0
CALL_TIMEOUT = 300.0


class MCPError(RuntimeError):
    pass


@dataclass
class ServerConfig:
    name: str
    command: str = ""
    args: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    url: str = ""
    headers: dict = field(default_factory=dict)
    tools: list | None = None          # allowlist; None = all
    enabled: bool = True
    source: str = ""


def _expand(value: str) -> str:
    """${VAR} / ${VAR:-default} expansion, like Claude Code's .mcp.json."""
    def sub(m):
        name, _, default = m.group(1).partition(":-")
        return os.environ.get(name, default)
    return re.sub(r"\$\{([^}]+)\}", sub, value) if isinstance(value, str) else value


def _from_entry(name: str, entry: dict, source: str) -> ServerConfig:
    return ServerConfig(
        name=name, command=_expand(entry.get("command", "")),
        args=[_expand(a) for a in entry.get("args", [])],
        env={k: _expand(v) for k, v in (entry.get("env") or {}).items()},
        url=_expand(entry.get("url", "")),
        headers={k: _expand(v) for k, v in (entry.get("headers") or {}).items()},
        tools=entry.get("tools"), enabled=entry.get("enabled", True), source=source)


def load_configs(cwd: str, config_mcp: dict | None = None) -> list[ServerConfig]:
    servers: dict[str, ServerConfig] = {}
    from .config import home_config_dir
    for path in (home_config_dir() / "mcp.json", Path(cwd) / ".mcp.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for name, entry in (data.get("mcpServers") or {}).items():
            if isinstance(entry, dict):
                servers[name] = _from_entry(name, entry, str(path))
    for name, entry in ((config_mcp or {}).get("servers") or {}).items():
        if isinstance(entry, dict):
            servers[name] = _from_entry(name, entry, "config.toml")
    return [s for s in servers.values() if s.enabled and (s.command or s.url)]


# ---------------------------------------------------------------- transports

class _Stdio:
    def __init__(self, cfg: ServerConfig):
        env = {**os.environ, **cfg.env}
        self.proc = subprocess.Popen([cfg.command, *cfg.args], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1, env=env)
        self.pending: dict[int, queue.Queue] = {}
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue                       # servers sometimes log to stdout
            if "id" in msg and ("result" in msg or "error" in msg):
                box = self.pending.pop(msg["id"], None)
                if box:
                    box.put(msg)
            elif "id" in msg and "method" in msg:      # server -> client request
                reply = {"jsonrpc": "2.0", "id": msg["id"]}
                if msg["method"] == "ping":
                    reply["result"] = {}
                else:
                    reply["error"] = {"code": -32601, "message": "not supported by hexiel"}
                self._write(reply)
        for box in list(self.pending.values()):          # process died
            box.put({"error": {"message": "server exited"}})

    def _write(self, msg: dict) -> None:
        with self.lock:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()

    def request(self, msg: dict, timeout: float) -> dict:
        box: queue.Queue = queue.Queue()
        self.pending[msg["id"]] = box
        try:
            self._write(msg)
        except (BrokenPipeError, OSError) as exc:
            raise MCPError(f"server not running: {exc}") from exc
        try:
            return box.get(timeout=timeout)
        except queue.Empty:
            self.pending.pop(msg["id"], None)
            raise MCPError(f"timed out after {timeout:.0f}s")

    def notify(self, msg: dict) -> None:
        self._write(msg)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class _Http:
    def __init__(self, cfg: ServerConfig):
        self.url = cfg.url
        self.headers = {"Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json", **cfg.headers}
        self.client = httpx.Client(timeout=CALL_TIMEOUT)

    def _post(self, msg: dict, timeout: float) -> httpx.Response:
        resp = self.client.post(self.url, json=msg, headers=self.headers, timeout=timeout)
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.headers["Mcp-Session-Id"] = sid
        if resp.status_code >= 400:
            raise MCPError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp

    def request(self, msg: dict, timeout: float) -> dict:
        resp = self._post(msg, timeout)
        if "text/event-stream" in resp.headers.get("content-type", ""):
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    try:
                        data = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if data.get("id") == msg["id"]:
                        return data
            raise MCPError("no response in event stream")
        return resp.json()

    def notify(self, msg: dict) -> None:
        self._post(msg, 30)

    def close(self) -> None:
        self.client.close()


# ---------------------------------------------------------------- server sessions

class Server:
    def __init__(self, cfg: ServerConfig):
        self.cfg = cfg
        self.ids = itertools.count(1)
        self.transport = None
        self.tools: list[dict] = []
        self.error = ""
        self.info: dict = {}

    def _rpc(self, method: str, params: dict | None = None, timeout: float = CALL_TIMEOUT) -> dict:
        msg = {"jsonrpc": "2.0", "id": next(self.ids), "method": method, "params": params or {}}
        reply = self.transport.request(msg, timeout)
        if "error" in reply:
            raise MCPError(reply["error"].get("message", str(reply["error"])))
        return reply.get("result", {})

    def start(self) -> "Server":
        try:
            self.transport = _Http(self.cfg) if self.cfg.url else _Stdio(self.cfg)
            init = self._rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                            "clientInfo": {"name": "hexiel", "version": __version__}},
                             timeout=START_TIMEOUT)
            self.info = init.get("serverInfo", {})
            self.transport.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
            cursor = None
            while True:
                page = self._rpc("tools/list", {"cursor": cursor} if cursor else {}, timeout=START_TIMEOUT)
                self.tools += page.get("tools", [])
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            if self.cfg.tools is not None:
                allowed = set(self.cfg.tools)
                self.tools = [t for t in self.tools if t.get("name") in allowed]
        except (MCPError, OSError, httpx.HTTPError, ValueError) as exc:
            self.error = str(exc) or type(exc).__name__
            self.close()
        return self

    def call(self, tool: str, arguments: dict) -> dict:
        return self._rpc("tools/call", {"name": tool, "arguments": arguments or {}})

    def close(self) -> None:
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:  # noqa: BLE001
                pass


_RUNNING: list[Server] = []


@atexit.register
def _shutdown() -> None:
    for server in _RUNNING:
        server.close()


def start_servers(configs: list[ServerConfig]) -> list[Server]:
    """Start every server in parallel; failures are kept with .error set."""
    if not configs:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(configs))) as pool:
        servers = list(pool.map(lambda c: Server(c).start(), configs))
    _RUNNING.extend(s for s in servers if not s.error)
    return servers


# ---------------------------------------------------------------- hexiel tools

def tool_name(server: str, tool: str) -> str:
    raw = f"mcp__{server}__{tool}"
    return re.sub(r"[^A-Za-z0-9_-]", "_", raw)[:64]


def tool_defs(servers: list[Server]) -> list:
    from .tools import ToolDef, ToolResult, text_result
    defs = []
    for server in servers:
        if server.error:
            continue
        for tool in server.tools:
            def run(args, ctx, _server=server, _name=tool["name"]):
                try:
                    result = _server.call(_name, args)
                except MCPError as exc:
                    return text_result(f"error: MCP {_server.cfg.name}/{_name}: {exc}", True)
                texts, images = [], []
                for part in result.get("content", []):
                    kind = part.get("type")
                    if kind == "text":
                        texts.append(part.get("text", ""))
                    elif kind == "image" and part.get("data"):
                        images.append({"media_type": part.get("mimeType", "image/png"),
                                       "data": part["data"], "approx_tokens": 800})
                    elif kind == "resource":
                        res = part.get("resource", {})
                        texts.append(res.get("text") or f"[resource {res.get('uri', '')}]")
                if result.get("structuredContent") and not texts:
                    texts.append(json.dumps(result["structuredContent"], indent=2))
                from .tools import head_tail
                return ToolResult(text=head_tail("\n".join(texts) or "(no output)"),
                                  is_error=bool(result.get("isError")), images=images)
            readonly = bool((tool.get("annotations") or {}).get("readOnlyHint"))
            schema = tool.get("inputSchema") or {"type": "object", "properties": {}}
            desc = (tool.get("description") or tool["name"]).strip()
            defs.append(ToolDef(tool_name(server.cfg.name, tool["name"]),
                                f"[MCP {server.cfg.name}] {desc}"[:1024], schema,
                                readonly=readonly, run=run))
    return defs


def status_lines(servers: list[Server]) -> list[str]:
    lines = []
    for s in servers:
        kind = "http" if s.cfg.url else "stdio"
        if s.error:
            lines.append(f"✗ {s.cfg.name} ({kind}) — {s.error}")
        else:
            label = s.info.get("name") or s.cfg.name
            size = len(json.dumps([t.get("inputSchema", {}) for t in s.tools])) // 4
            lines.append(f"✓ {s.cfg.name} ({kind}, {label}) — {len(s.tools)} tool(s), ~{size} schema tokens"
                         + ("" if s.cfg.tools is None else " (allowlisted)"))
    return lines
