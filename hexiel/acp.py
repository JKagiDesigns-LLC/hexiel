"""hexiel as an Agent Client Protocol (ACP) agent: `hexiel --acp` speaks JSON-RPC 2.0 over stdio so Zed, JetBrains
IDEs and other ACP clients can use hexiel as their agent.

    Zed settings.json:   "agent_servers": { "hexiel": { "command": "hexiel", "args": ["--acp"] } }

Supported: initialize, authenticate (no-op: keys live in hexiel's own config), session/new (with the editor's MCP
servers), session/load (replays the saved conversation), session/prompt (text, file links, embedded files and images),
session/cancel, and session/set_mode (ask / auto). Streamed back as session/update: agent_message_chunk,
agent_thought_chunk, tool_call, tool_call_update and plan (hexiel's todo list). Tool approvals go to the editor as
session/request_permission. stdout carries only protocol messages; anything else hexiel prints goes to stderr."""

from __future__ import annotations

import base64
import itertools
import json
import os
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from urllib.parse import unquote, urlparse

PROTOCOL_VERSION = 1

TOOL_KINDS = {"read": "read", "file_info": "read", "outline": "read", "view_image": "read", "describe_image": "read",
              "write": "edit", "edit": "edit", "image": "edit", "video": "edit", "notebook": "edit",
              "grep": "search", "glob": "search", "list": "search", "map": "search", "find": "search",
              "where_is": "search", "callers": "search", "symbols": "search", "knowledge_search": "search",
              "bash": "execute", "python": "execute", "powershell": "execute", "test": "execute",
              "container": "execute", "tasks": "execute",
              "web_fetch": "fetch", "web_search": "fetch", "render": "fetch",
              "consult": "think", "task": "think", "todo": "think"}


class RpcError(Exception):
    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def _short(args: dict) -> str:
    for key in ("path", "command", "pattern", "query", "name", "target", "url", "description"):
        if args.get(key):
            return str(args[key]).replace("\n", " ")[:80]
    return ""


def _path_from_uri(uri: str) -> str:
    parsed = urlparse(uri)
    return unquote(parsed.path) if parsed.scheme in ("file", "") else uri


class Server:
    """One ACP connection. `build(cwd, resumed)` makes a hexiel Session (main.build_session by default)."""

    def __init__(self, reader=None, writer=None, build=None, model: str | None = None):
        self.reader = reader or sys.stdin.buffer
        self.writer = writer or sys.stdout.buffer
        self.build = build or self._default_build
        self.write_lock = threading.Lock()
        self.ids = itertools.count(1)
        self.pending: dict = {}                    # our request id -> [Event, response]
        self.sessions: dict = {}                   # sessionId -> Session
        self.running: dict = {}                    # sessionId -> Thread of the prompt in progress
        self.call_ids: dict = {}                   # (sessionId, tool, args json) -> the id the editor already knows
        self.client_caps: dict = {}
        self.model = model                         # `hexiel --acp -m PROFILE` pins the model for every session

    # ---------------------------------------------------------------- transport
    def send(self, msg: dict) -> None:
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        with self.write_lock:
            self.writer.write(data)
            self.writer.flush()

    def notify(self, method: str, params: dict) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict, timeout: float | None = None):
        rid = f"h{next(self.ids)}"
        slot = [threading.Event(), None]
        self.pending[rid] = slot
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not slot[0].wait(timeout):
            self.pending.pop(rid, None)
            raise TimeoutError(method)
        msg = self.pending.pop(rid)[1] or {}
        if "error" in msg:
            raise RpcError(msg["error"].get("code", -32000), msg["error"].get("message", "error"))
        return msg.get("result")

    def update(self, sid: str, update: dict) -> None:
        self.notify("session/update", {"sessionId": sid, "update": update})

    def serve(self) -> None:
        for raw in iter(self.reader.readline, b""):
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            if "method" not in msg:                  # a response to one of our requests
                slot = self.pending.get(msg.get("id"))
                if slot:
                    slot[1] = msg
                    slot[0].set()
                continue
            if msg["method"] == "session/prompt":    # long: its own thread, answered when the turn ends
                threading.Thread(target=self._dispatch, args=(msg,), daemon=True, name="acp-prompt").start()
            else:
                self._dispatch(msg)
        for slot in list(self.pending.values()):     # the editor went away: unblock any waiting approval
            slot[0].set()

    def _dispatch(self, msg: dict) -> None:
        method, params, rid = msg["method"], msg.get("params") or {}, msg.get("id")
        handler = getattr(self, "on_" + method.replace("/", "_"), None)
        try:
            if handler is None:
                raise RpcError(-32601, f"method not found: {method}")
            result = handler(params)
            if rid is not None:
                self.send({"jsonrpc": "2.0", "id": rid, "result": result})
        except RpcError as exc:
            if rid is not None:
                self.send({"jsonrpc": "2.0", "id": rid, "error": {"code": exc.code, "message": exc.message,
                                                                    **({"data": exc.data} if exc.data else {})}})
        except Exception as exc:  # noqa: BLE001 - one failed request must not end the connection
            traceback.print_exc(file=sys.stderr)
            if rid is not None:
                from .explain import crash
                self.send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": crash(exc)}})

    # ---------------------------------------------------------------- lifecycle
    def on_initialize(self, params: dict) -> dict:
        from . import __version__
        self.client_caps = params.get("clientCapabilities") or {}
        return {"protocolVersion": PROTOCOL_VERSION,
                "agentCapabilities": {"loadSession": True,
                                      "promptCapabilities": {"image": True, "audio": False, "embeddedContext": True},
                                      "mcpCapabilities": {"http": True, "sse": True}},
                "authMethods": [],
                "agentInfo": {"name": "hexiel", "title": "hexiel", "version": __version__}}

    def on_authenticate(self, params: dict) -> dict:
        return {}

    def _default_build(self, cwd: str, resumed: dict | None):
        from .main import build_session
        return build_session(cwd, self.model, resumed=resumed)

    def _attach(self, session, sid: str, mcp: list) -> None:
        session.on_event = lambda event, *p, **kw: self._event(sid, event, p, kw)
        session.on_approve = lambda name, args: self._approve(sid, name, args)
        if mcp:
            self._add_mcp(session, mcp)
        self.sessions[sid] = session

    def _add_mcp(self, session, servers: list) -> None:
        """The editor's MCP servers (stdio, http or sse) join this session's tools."""
        try:
            from . import mcp_client
            configs = []
            for s in servers:
                entry = {"command": s["command"], "args": s.get("args") or [],
                         "env": {e["name"]: e["value"] for e in s.get("env") or []}} if s.get("command") else \
                    {"url": s.get("url"), "headers": {h["name"]: h["value"] for h in s.get("headers") or []}}
                configs.append(mcp_client._from_entry(s.get("name", "editor"), entry, "editor (ACP)"))
            started = mcp_client.start_servers(configs)
            known = {t.name for t in session.tools}
            session.tools.extend(t for t in mcp_client.tool_defs(started) if t.name not in known)
            session.mcp_servers = list(getattr(session, "mcp_servers", None) or []) + list(started)
        except Exception as exc:  # noqa: BLE001 - a broken MCP server must not fail the session
            print(f"hexiel acp: MCP servers from the editor not started: {exc}", file=sys.stderr)

    def on_session_new(self, params: dict) -> dict:
        cwd = params.get("cwd") or os.getcwd()
        if not os.path.isdir(cwd):
            raise RpcError(-32602, f"cwd is not a directory: {cwd}")
        session = self.build(cwd, None)
        sid = session.id
        self._attach(session, sid, params.get("mcpServers") or [])
        return {"sessionId": sid, "modes": self._modes(session)}

    def on_session_load(self, params: dict):
        from .config import home_data_dir
        from .session import strip_env_note
        sid = str(params.get("sessionId", ""))
        path = home_data_dir() / "sessions" / f"{Path(sid).name}.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            raise RpcError(-32602, f"no saved hexiel session {sid}") from None
        cwd = params.get("cwd") or data.get("cwd") or os.getcwd()
        session = self.build(cwd, data)
        self._attach(session, sid, params.get("mcpServers") or [])
        for m in session.messages:                     # replay: the editor shows the conversation again
            text = m.get("content")
            if not isinstance(text, str) or not text.strip():
                continue
            if m.get("role") == "user":
                shown = strip_env_note(text)
                if shown.strip() and not shown.startswith("[system]"):
                    self.update(sid, {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": shown}})
            elif m.get("role") == "assistant":
                self.update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})
        return None

    def _modes(self, session) -> dict:
        auto = bool(getattr(session, "auto_approve", False))
        return {"currentModeId": "auto" if auto else "ask",
                "availableModes": [{"id": "ask", "name": "Ask", "description": "ask before commands and file writes"},
                                   {"id": "auto", "name": "Auto", "description": "run tools without asking"}]}

    def on_session_set_mode(self, params: dict) -> dict:
        session = self._session(params)
        mode = params.get("modeId")
        if mode not in ("ask", "auto"):
            raise RpcError(-32602, f"unknown mode {mode}")
        session.auto_approve = mode == "auto"
        self.update(params["sessionId"], {"sessionUpdate": "current_mode_update", "currentModeId": mode})
        return {}

    def _session(self, params: dict):
        session = self.sessions.get(params.get("sessionId"))
        if session is None:
            raise RpcError(-32602, f"unknown session {params.get('sessionId')}")
        return session

    # ---------------------------------------------------------------- prompts
    def _prompt_text(self, sid: str, blocks: list) -> str:
        parts = []
        for b in blocks or []:
            kind = b.get("type")
            if kind == "text":
                parts.append(b.get("text", ""))
            elif kind == "resource_link":
                parts.append(f"@{_path_from_uri(b.get('uri', ''))}")
            elif kind == "resource":
                res = b.get("resource") or {}
                name = _path_from_uri(res.get("uri", "")) or "attached"
                if "text" in res:
                    parts.append(f"\n{name}:\n```\n{res['text']}\n```")
                elif res.get("blob"):
                    parts.append(f"@{self._save_blob(sid, res['blob'], Path(name).suffix or '.bin')}")
            elif kind == "image":
                ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}.get(
                    b.get("mimeType", ""), ".png")
                parts.append(f"@{self._save_blob(sid, b.get('data', ''), ext)}")
        return "\n".join(p for p in parts if p).strip()

    def _save_blob(self, sid: str, b64: str, ext: str) -> str:
        folder = Path(tempfile.gettempdir()) / "hexiel-acp" / sid
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"attachment-{next(self.ids)}{ext}"
        path.write_bytes(base64.b64decode(b64))
        return str(path)

    def on_session_prompt(self, params: dict) -> dict:
        session = self._session(params)
        sid = params["sessionId"]
        if sid in self.running and self.running[sid].is_alive():
            raise RpcError(-32000, "a prompt is already running in this session")
        text = self._prompt_text(sid, params.get("prompt"))
        if not text:
            return {"stopReason": "end_turn"}
        state = {"aborted": False, "error": None}
        session.__dict__["_acp_turn"] = state
        self.running[sid] = threading.current_thread()
        try:
            session.run_turn(text)
        except Exception as exc:  # noqa: BLE001 - said in the chat, the turn ends normally
            from .explain import crash
            self.update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": f"\n\n⚠ {crash(exc)}"}})
        finally:
            self.running.pop(sid, None)
        return {"stopReason": "cancelled" if state["aborted"] or session._stop.is_set() else "end_turn"}

    def on_session_cancel(self, params: dict) -> None:
        session = self.sessions.get(params.get("sessionId"))
        if session is not None:
            session.stop()
        for slot in list(self.pending.values()):       # an approval waiting in the editor: treated as "no"
            slot[1] = {"result": {"outcome": {"outcome": "cancelled"}}}
            slot[0].set()

    # ---------------------------------------------------------------- hexiel events → session/update
    def _call_id(self, sid: str, name: str, args: dict, fallback: str) -> str:
        key = (sid, name, json.dumps(args, sort_keys=True, default=str))
        return self.call_ids.pop(key, None) or fallback

    def _event(self, sid: str, event: str, payload: tuple, kw: dict) -> None:
        if event == "text" and payload:
            self.update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": payload[0]}})
        elif event == "thinking" and payload:
            self.update(sid, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": payload[0]}})
        elif event == "note" and payload:
            self.update(sid, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": f"{payload[0]}\n"}})
        elif event == "tool_call" and payload:
            call = payload[0]
            args = call.get("arguments") or {}
            cid = self._call_id(sid, call.get("name", ""), args, call.get("id") or f"call-{next(self.ids)}")
            call["_acp_id"] = cid
            update = {"sessionUpdate": "tool_call", "toolCallId": cid,
                      "title": f"{call.get('name')} {_short(args)}".strip(),
                      "kind": TOOL_KINDS.get(call.get("name", ""), "other"), "status": "in_progress", "rawInput": args}
            if args.get("path"):
                update["locations"] = [{"path": str(Path(self.sessions[sid].cwd, args["path"]))}]
            self.update(sid, update)
        elif event == "tool_result" and len(payload) >= 2:
            call, text = payload[0], str(payload[1])
            failed = bool(payload[2]) if len(payload) > 2 else False
            if not call.get("_acp_id"):                # never announced (e.g. an unknown tool): open its card first
                self._event(sid, "tool_call", (call,), {})
            cid = call.get("_acp_id") or call.get("id")
            content = [{"type": "content", "content": {"type": "text", "text": text[:8000]}}]
            args = call.get("arguments") or {}
            if call.get("name") == "edit" and not failed and "old_string" in args:
                content.insert(0, {"type": "diff", "path": str(Path(self.sessions[sid].cwd, args.get("path", ""))),
                                   "oldText": args.get("old_string", ""), "newText": args.get("new_string", "")})
            elif call.get("name") == "write" and not failed:
                content.insert(0, {"type": "diff", "path": str(Path(self.sessions[sid].cwd, args.get("path", ""))),
                                   "oldText": None, "newText": args.get("content", "")})
            self.update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": cid,
                              "status": "failed" if failed else "completed", "content": content})
            if call.get("name") == "todo":
                self._plan(sid)
        elif event == "turn_end":
            state = self.sessions[sid].__dict__.get("_acp_turn") or {}
            info = payload[0] if payload and isinstance(payload[0], dict) else {}
            state["aborted"] = bool(kw.get("aborted"))
            if info.get("error") and info.get("kind") not in ("loop_guard", "budget"):
                state["error"] = info["error"]
                self.update(sid, {"sessionUpdate": "agent_message_chunk",
                                  "content": {"type": "text", "text": f"\n\n⚠ {info['error']}"}})

    def _plan(self, sid: str) -> None:
        from . import todo
        try:
            _title, _left, items = todo.read_todos(self.sessions[sid].cwd)
        except Exception:  # noqa: BLE001
            return
        status = {" ": "pending", "~": "in_progress", "x": "completed"}
        self.update(sid, {"sessionUpdate": "plan", "entries": [
            {"content": it.text, "priority": "medium", "status": status.get(it.state, "pending")} for it in items]})

    def _approve(self, sid: str, name: str, args: dict) -> str:
        cid = f"call-{next(self.ids)}"
        self.call_ids[(sid, name, json.dumps(args, sort_keys=True, default=str))] = cid
        params = {"sessionId": sid,
                  "toolCall": {"toolCallId": cid, "title": f"{name} {_short(args if isinstance(args, dict) else {})}".strip(),
                               "kind": TOOL_KINDS.get(name, "other"), "status": "pending", "rawInput": args},
                  "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                              {"optionId": "always", "name": "Always allow this session", "kind": "allow_always"},
                              {"optionId": "reject", "name": "Reject", "kind": "reject_once"}]}
        try:
            result = self.request("session/request_permission", params) or {}
        except (TimeoutError, RpcError):
            return "no"
        outcome = result.get("outcome") or {}
        if outcome.get("outcome") != "selected":
            return "no"
        return {"allow": "yes", "always": "always"}.get(outcome.get("optionId"), "no")


def main(model: str | None = None) -> int:
    """`hexiel --acp [-m PROFILE]`: serve one ACP connection on stdin/stdout until the editor closes it."""
    out = sys.stdout.buffer
    sys.stdout = sys.stderr                       # protocol only on the real stdout; stray prints go to stderr
    Server(sys.stdin.buffer, out, model=model).serve()
    return 0
