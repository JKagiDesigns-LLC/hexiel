"""hexiel Web UI server (OpenWebUI-like): aiohttp app serving the chat SPA,
streaming agent events over WebSocket, approval bridging via browser dialogs,
and an OpenAI-compatible /v1/chat/completions endpoint (read-only tools) for
OpenWebUI "connections" and other clients.

The server shares the same Session object the TUI uses, so `hexiel serve` and
`hexiel` stay in sync on messages, todo state and incidents."""

import asyncio
import sys
import json
import threading
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from . import memory as memory_mod
from . import settings_edit
from . import skills as skills_mod
from . import todo as todo_mod
from .config import load_config, home_data_dir
from .llm import make_provider
from .session import Session
from .tooling import detect_toolchain
from .tools import ToolResult

WEB_DIR = Path(__file__).parent / "web"


def _shorten(text: str, limit: int = 800) -> str:
    return text[:limit] + ("…" if len(text) > limit else "")


def short_call(call: dict) -> dict:
    args = call.get("arguments", {})
    for key in ("command", "path", "pattern", "url", "query", "action", "title", "name"):
        if key in args:
            return {"id": call.get("id", ""), "name": call["name"],
                    "short": str(args[key]).replace("\n", " ⏎ ")[:80]}
    return {"id": call.get("id", ""), "name": call["name"],
            "short": json.dumps(args)[:80]}


def status_state(session: Session) -> dict:
    tokens_now = session.tokens_now()
    window = session.model_config.context_window
    return {
        "model": session.model_config.name,
        "model_id": session.model_config.model,
        "context": {"tokens": tokens_now, "window": window,
                    "pct": min(100, 100 * tokens_now // max(window, 1)),
                    "compact_at_pct": int(100 * session.settings.compact_threshold)},
        "auto_approve": session.auto_approve,
        "busy": getattr(session, "_web_busy", False),
        "todo": todo_mod.summary_line(session.cwd) or None,
    }


class WebState:
    """Server-side canonical chat view, shared by all browser tabs."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.clients: dict[web.WebSocketResponse, asyncio.AbstractEventLoop] = {}
        self.approvals: dict[int, dict] = {}   # id -> {"event": Event, "verdict": str}
        self.lock = threading.Lock()
        self.busy = False
        self.queue: "queue.Queue[bytes]" = __import__("queue").Queue()
        session.on_event = self.on_event
        session.on_approve = self.ask_approval

    def broadcast(self, message: dict) -> None:
        # called from worker threads: serialize onto the queue; the drain task
        # (started per app) delivers to every connected tab
        try:
            self.queue.put(json.dumps(message))
        except Exception:
            pass

    async def drain(self, app) -> None:
        """Event-loop task: pull messages pushed by worker threads and send them
        to every websocket. Plain awaits — no cross-thread scheduling.
        `queue.get` runs in a thread because broadcast() may come from worker
        threads; None is the shutdown sentinel that lets cancellation land."""
        loop = asyncio.get_running_loop()
        while True:
            payload = await loop.run_in_executor(None, self.queue.get)
            if payload is None:
                return
            with self.lock:
                clients = list(self.clients.keys())
            for ws in clients:
                try:
                    await ws.send_str(payload)
                except Exception:
                    self.clients.pop(ws, None)

    # -- session event fan-out

    def on_event(self, event, *payload, **_kw) -> None:
        if event == "text":
            self.broadcast({"t": "text", "delta": payload[0]})
        elif event == "thinking":
            self.broadcast({"t": "thinking", "delta": payload[0]})
        elif event == "rate_limited":
            self.broadcast({"t": "note", "text": f"⏸ rate limited — resuming automatically in "
                                                f"~{int(payload[0] // 60)}m{int(payload[0] % 60):02d}s"})
        elif event == "fallback":
            self.broadcast({"t": "note", "text": f"↪ {payload[0]} is limited — continuing on {payload[1]}"})
        elif event == "turn_end":
            error = payload[0].get("error") if payload and isinstance(payload[0], dict) else None
            if _kw.get("aborted"):
                error = "stopped by user"
            self.broadcast({"t": "turn_end", "error": error,
                            "messages": self.render_history()})
        elif event == "tool_call":
            self.broadcast({"t": "tool", **short_call(payload[0])})
        elif event == "tool_result":
            call, text, is_error = payload[0], payload[1], payload[2]
            elapsed = payload[3] if len(payload) > 3 else 0.0
            self.broadcast({"t": "tool_done", "id": call.get("id", ""),
                            "ok": not is_error, "elapsed": round(elapsed, 1),
                            "preview": _shorten(text)})
        elif event == "compact_start":
            self.broadcast({"t": "note", "text": "Compacting context (summarizing history)…"})
        elif event == "compact_end":
            stats = payload[0] if payload else {}
            if stats.get("before") is not None:
                self.broadcast({"t": "note", "text":
                                f"✓ compacted: {stats.get('before', 0) // 1000}k → "
                                f"{stats.get('after', 0) // 1000}k tokens"})
            elif stats.get("elided"):
                self.broadcast({"t": "note", "text":
                                f"✓ saved ~{stats['elided']} tool outputs from context — "
                                "no compaction needed"})
        elif event == "incident":
            self.broadcast({"t": "incident", "text": _shorten(str(payload[-1]), 300)})
        elif event == "usage":
            self.broadcast({"t": "status", "status": status_state(self.session)})

    def ask_approval(self, tool_name: str, args) -> str:
        request_id = time.time_ns() % 10 ** 9
        holder = {"event": threading.Event(), "verdict": "no"}
        self.approvals[request_id] = holder
        self.broadcast({"t": "approval", "id": request_id, "tool": tool_name,
                        "args": json.dumps(args, indent=2)[:1400]})
        holder["event"].wait(timeout=900)
        self.approvals.pop(request_id, None)
        verdict = holder["verdict"] if holder["verdict"] in ("yes", "always", "no") else "no"
        self.broadcast({"t": "approval_done", "id": request_id, "verdict": verdict})
        return verdict

    # -- history rendering

    def render_history(self) -> list[dict]:
        items: list[dict] = []
        pending: dict[str, dict] = {}
        for msg in self.session.messages:
            role = msg["role"]
            if role == "user":
                body = msg.get("content", "")
                if body.startswith("<auto-compaction>"):
                    items.append({"t": "note", "text": "— earlier history auto-compacted —"})
                else:
                    items.append({"t": "user", "text": body})
            elif role == "assistant":
                # the model's text precedes the tool calls it then makes
                text = msg.get("content", "")
                if text.strip():
                    items.append({"t": "assistant", "text": text})
                for call in msg.get("tool_calls", []):
                    pending[call["id"]] = {"id": call["id"], "t": "tool", **short_call(call)}
                    items.append(pending[call["id"]])
            elif role == "tool":
                entry = pending.get(msg.get("tool_call_id", ""))
                if entry is not None:
                    entry["ok"] = not msg.get("is_error")
                    entry["preview"] = _shorten(msg.get("content", ""))
        return items


def _json_response(data: dict, status: int = 200) -> web.Response:
    return web.Response(text=json.dumps(data), content_type="application/json",
                        status=status)


def make_app(session: Session) -> web.Application:
    state = WebState(session)
    app = web.Application(client_max_size=32 * 1024 * 1024)
    app["state"] = state

    async def start_drain(app):
        app["drain_task"] = asyncio.create_task(state.drain(app))

    async def stop_drain(app):
        state.queue.put(None)          # unblock the executor get so it can exit
        task = app.get("drain_task")
        if task:
            try:
                await asyncio.wait_for(asyncio.shield(task), 5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
    app.on_startup.append(start_drain)
    app.on_cleanup.append(stop_drain)

    async def index(request):
        return web.FileResponse(WEB_DIR / "index.html")

    async def static(request):
        path = (WEB_DIR / request.match_info["name"]).resolve()
        if not str(path).startswith(str(WEB_DIR)) or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def websocket(request):
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=64 * 1024 * 1024)
        await ws.prepare(request)
        state.clients[ws] = asyncio.get_running_loop()
        try:
            await ws.send_str(json.dumps({"t": "history",
                                          "items": state.render_history(),
                                          "status": status_state(session)}))
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        handle_ws_message(json.loads(msg.data))
                    except (ValueError, KeyError, TypeError):
                        pass
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            state.clients.pop(ws, None)
        return ws

    heal = {"ws": None, "incidents": [], "description": "", "changes": None}

    def heal_review_payload() -> dict:
        changes = heal["changes"] or []
        return {"t": "heal_review",
                "summary": f"{len(changes)} file(s) changed, +{sum(c.added for c in changes)} "
                           f"−{sum(c.removed for c in changes)}",
                "files": [{"path": c.path, "status": c.status, "added": c.added, "removed": c.removed,
                           "diff": c.diff[:60000]} for c in changes]}

    def start_heal(description: str) -> str:
        from . import contribute, heal as heal_mod
        from .heal import heal_prompt
        from .main import build_session
        if heal["ws"] is not None:
            return "a self-heal fix is already in progress or waiting — /heal review"
        incidents = heal_mod.unhealed(session.cwd)

        def run():
            try:
                ws = contribute.prepare()
            except Exception as exc:  # noqa: BLE001
                state.broadcast({"t": "note", "text": f"could not prepare the heal workspace: {exc}"})
                return
            heal.update(ws=ws, incidents=incidents, description=description)
            try:
                healer = build_session(str(ws.path), session.model_config.name)
            except Exception:  # noqa: BLE001
                healer = build_session(str(ws.path), None)
            healer.model_config = session.model_config
            healer.auto_approve = session.auto_approve
            healer.on_event = state.on_event
            healer.on_approve = state.ask_approval
            with state.lock:
                state.busy = True
            try:
                healer.run_turn(heal_prompt(incidents, str(ws.path), description, sys.executable))
            finally:
                with state.lock:
                    state.busy = False
            heal["changes"] = contribute.changes(ws)
            if not heal["changes"]:
                contribute.discard(ws)
                heal.update(ws=None, changes=None)
                state.broadcast({"t": "note", "text": "self-heal made no changes"})
            else:
                state.broadcast(heal_review_payload())
        threading.Thread(target=run, daemon=True).start()
        return (f"🔧 self-heal: {len(incidents)} recorded incident(s) — fixing hexiel in an isolated copy of "
                "its source; your project is untouched. You'll review the diff before anything is kept.")

    def web_command(text: str) -> str | None:
        """Slash commands typed in the browser. Returns a note to show, or None
        when the text should go to the model (skills / custom commands render to
        a prompt and are returned via the 'prompt:' prefix)."""
        from . import commands as commands_mod, jobs as jobs_mod
        cmd, _, arg = text[1:].partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        if cmd == "help":
            return "\n".join(f"/{n} {a}".ljust(24) + d for n, a, d in commands_mod.BUILTIN)
        if cmd == "undo":
            restored = session.undo()
            return ("↶ " + "\n".join(restored)) if restored else "nothing to undo"
        if cmd == "plan":
            session.plan_mode = not session.plan_mode
            return "📝 plan mode ON — investigate and plan, change nothing" if session.plan_mode else "plan mode OFF"
        if cmd == "usage":
            t = session.usage_totals
            return (f"{t['requests']} request(s) · input {t['prompt_tokens']:,} ({t['cached_tokens']:,} cached) · "
                    f"output {t['completion_tokens']:,} tokens")
        if cmd == "permissions":
            r = session.rules
            return "\n".join(f"{k}: " + (", ".join(getattr(r, k)) or "—") for k in ("deny", "ask", "allow"))
        if cmd == "jobs":
            return "\n".join(jobs_mod.describe(j) for j in jobs_mod.load()) or "no scheduled jobs"
        if cmd == "inbox":
            box = jobs_mod.inbox_dir()
            files = sorted(box.glob("*.md"), reverse=True)[:3] if box.exists() else []
            return "\n\n".join(f.read_text()[:3000] for f in files) or "the inbox is empty"
        if cmd == "mcp":
            from . import mcp_client
            if arg.startswith("add"):
                name = arg[3:].strip().lower()
                if name not in mcp_client.PRESETS:
                    return "MCP presets: " + ", ".join(mcp_client.PRESETS)
                cfg, note = mcp_client.add_preset(name, session.cwd)
                started = mcp_client.start_servers([cfg])
                if started and not started[0].error:
                    session.mcp_servers.extend(started)
                    session.tools.extend(mcp_client.tool_defs(started))
                return "\n".join(mcp_client.status_lines(started)) + f"\n{note}"
            return "\n".join(mcp_client.status_lines(session.mcp_servers)) or "no MCP servers — /mcp add <preset>"
        if cmd == "clear":
            session.messages = []
            session.last_usage = {}
            return "conversation cleared"
        if cmd == "heal":
            if arg == "review":
                if heal["changes"]:
                    state.broadcast(heal_review_payload())
                    return "reopened the self-heal review"
                return "no self-heal fix is waiting for review"
            return start_heal(arg)
        if cmd == "ship":
            return "/ship uses the terminal UI's step-by-step approvals — run it there (hexiel)"
        custom = commands_mod.discover(session.cwd)
        if cmd in custom:
            return "prompt:" + custom[cmd].render(arg)
        if cmd in {s.name.lower() for s in skills_mod.discover(session.cwd)}:
            return (f'prompt:Use the "{cmd}" skill for this request: {arg or text}\n'
                    "(invoke it with the skill tool and follow its instructions)")
        if cmd in {n for n, _, _ in commands_mod.BUILTIN}:
            return f"/{cmd} isn't available in the browser yet — use the terminal UI"
        return f"unknown command /{cmd} — /help lists them"

    def handle_ws_message(data: dict) -> None:
        if data.get("t") == "send":
            text = str(data.get("text", "")).strip()
            if text.startswith("/") and not state.busy:
                reply = web_command(text)
                if reply and not reply.startswith("prompt:"):
                    state.broadcast({"t": "note", "text": reply})
                    state.broadcast({"t": "status", "status": status_state(session)})
                    return
                if reply:
                    text = reply[len("prompt:"):]
            with state.lock:
                if not text or state.busy:
                    if text:
                        state.broadcast({"t": "note",
                                         "text": "hexiel is still working — stop it first"})
                    return
                state.busy = True
                def run():
                    try:
                        session.run_turn(text)
                    finally:
                        with state.lock:
                            state.busy = False
                        state.broadcast({"t": "status", "status": status_state(session)})
                threading.Thread(target=run, daemon=True).start()
        elif data.get("t") == "stop":
            session.stop()
        elif data.get("t") == "approve":
            holder = state.approvals.get(int(data.get("id", 0)))
            if holder:
                holder["verdict"] = data.get("verdict", "no")
                holder["event"].set()

    async def api_heal_decide(request):
        from . import contribute, heal as heal_mod, updater
        data = await request.json()
        choice = data.get("choice", "later")
        if heal["ws"] is None or not heal["changes"]:
            return _json_response({"error": "no self-heal fix is waiting"}, 400)
        if choice == "later":
            return _json_response({"ok": True, "message": "kept for later — /heal review to decide"})
        if choice == "discard":
            contribute.discard(heal["ws"])
            heal.update(ws=None, changes=None)
            return _json_response({"ok": True, "message": "self-heal fix discarded"})
        wanted = set(data.get("files") or [])
        chosen = [c for c in heal["changes"] if c.path in wanted]
        if not chosen:
            return _json_response({"error": "select at least one file"}, 400)

        def apply() -> str:
            contribute.keep(heal["ws"], chosen)
            msg = f"applied {len(chosen)} file(s) to your hexiel"
            if choice == "send":
                title = "self-heal: " + (" ".join((heal["description"] or next(
                    (str(i.get("detail", "")) for i in heal["incidents"]), "")).split())[:70] or "fix")
                sent = contribute.send(heal["ws"], chosen, title,
                                       contribute.mr_body(heal["description"], heal["incidents"], chosen))
                msg += f"; sent to hexiel for review: {sent['mr_url']} — thank you!"
            heal_mod.mark_healed(session.cwd)
            contribute.discard(heal["ws"])
            heal.update(ws=None, changes=None)
            return msg
        loop = asyncio.get_running_loop()
        try:
            msg = await loop.run_in_executor(None, apply)
        except Exception as exc:  # noqa: BLE001 - e.g. no GitLab token: the fix is still kept locally
            return _json_response({"error": f"{exc}", "kept": True})
        session.save()
        loop.call_later(1.0, updater.restart)        # load the fixed code; the page reconnects
        return _json_response({"ok": True, "message": msg, "restarting": True})

    async def api_commands(request):
        from . import commands as commands_mod
        items = [{"name": n, "args": a, "desc": d} for n, a, d in commands_mod.BUILTIN]
        items += [{"name": c.name, "args": c.argument_hint, "desc": c.description[:80]}
                  for c in commands_mod.discover(session.cwd).values()]
        items += [{"name": s.name, "args": "<request>", "desc": "skill: " + (s.description or "")[:70]}
                  for s in skills_mod.discover(session.cwd)]
        return _json_response({"commands": items})

    async def api_paste_image(request):
        from .clipboard import save_image
        mime = request.content_type
        if mime not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
            return _json_response({"error": "unsupported image type"}, status=400)
        data = await request.read()
        if not data or len(data) > 20 * 1024 * 1024:
            return _json_response({"error": "empty or over 20MB"}, status=400)
        return _json_response({"path": save_image(data, mime, session.cwd)})

    async def api_state(request):
        cfg = load_config()
        chain = detect_toolchain(session.cwd)
        sessions = []
        try:
            paths = sorted((home_data_dir() / "sessions").glob("*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:200]
            for p in paths:
                if len(sessions) >= 12:
                    break
                try:
                    raw = json.loads(p.read_text())
                    if raw.get("cwd") != session.cwd:      # this project's sessions only
                        continue
                    preview = next((m.get("content", "")[:60] for m in raw.get("messages", [])
                                    if m.get("role") == "user"
                                    and not m.get("content", "").startswith("<auto-compaction>")), "")
                    sessions.append({"id": raw.get("id", p.stem), "created": raw.get("created"),
                                     "model": raw.get("model"), "preview": preview})
                except (OSError, json.JSONDecodeError, KeyError):
                    continue
        except OSError:
            pass
        _, _, todos = todo_mod.read_todos(session.cwd)
        skills = [{"name": s.name, "description": s.description[:160]}
                  for s in skills_mod.discover(session.cwd)]
        return _json_response({
            "status": status_state(session),
            "models": sorted(cfg.models),
            "current_profile": session.model_config.name,
            "settings": {"auto_approve": session.auto_approve,
                         "compact_threshold": session.settings.compact_threshold},
            "todo": [{"text": i.text, "state": i.state} for i in todos],
            "memory_index": memory_mod.index_text(session.cwd)[:4000],
            "sessions": sessions,
            "skills": skills,
            "toolchain": {"container": chain.container.binary if chain.container else None,
                          "test_runners": [r.label for r in chain.test_runners]},
        })

    catalog: dict[str, object] = {}       # id -> Found, from the latest live discovery

    async def api_models_live(request):
        """Every model reachable right now (queried fresh, in parallel)."""
        from . import model_catalog
        groups, errors = {}, {}

        def collect(ep, found, err):
            if err:
                errors[ep.label] = err
            else:
                groups[ep.label] = found
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: model_catalog.discover(load_config(), collect))
        catalog.clear()
        out = []
        current = session.model_config.name
        for label in sorted(groups, key=lambda l: (not any(f.profile == current for f in groups[l]),
                                                   "Ollama" not in l, l)):
            items = []
            for i, f in enumerate(sorted(groups[label], key=lambda f: (not f.profile, f.model))):
                key = f"{label}|{f.model}"
                catalog[key] = f
                items.append({"id": key, "model": f.model, "profile": f.profile, "detail": f.detail,
                              "current": bool(f.profile) and f.profile == current})
            out.append({"label": label, "models": items})
        return _json_response({"groups": out, "errors": errors})

    async def api_models_select(request):
        from . import model_catalog
        from .config import ModelConfig
        data = await request.json()
        found = catalog.get(data.get("id", ""))
        scope = data.get("scope", "session")
        if found is None:
            return _json_response({"error": "model list is out of date — reopen the picker"}, 400)
        name, fields = model_catalog.profile_for(found)
        try:
            if fields and scope == "session":
                session.model_config = ModelConfig(name=name, **fields)
                msg = f"Model → {found.model} (this session only)"
            else:
                if fields:
                    try:
                        settings_edit.add_profile(name, fields)
                    except ValueError:
                        pass
                msg = settings_edit.apply(session, "profile", name, scope)
        except (KeyError, ValueError, AssertionError, SystemExit) as exc:
            return _json_response({"error": str(exc)}, 400)
        state.broadcast({"t": "status", "status": status_state(session)})
        return _json_response({"ok": True, "message": msg})

    async def api_model(request):
        data = await request.json()
        try:
            settings_edit.apply(session, "profile", data.get("name", ""), data.get("scope", "session"))
        except (KeyError, ValueError) as exc:
            return _json_response({"error": str(exc)}, 400)
        state.broadcast({"t": "status", "status": status_state(session)})
        return _json_response({"ok": True})

    async def api_settings(request):
        # legacy session-only toggles; /api/config is the full editor
        data = await request.json()
        for key, field in (("auto_approve", "settings.auto_approve"),
                           ("compact_threshold", "settings.compact_threshold")):
            if key in data:
                settings_edit.apply(session, field, data[key], "session")
        state.broadcast({"t": "status", "status": status_state(session)})
        return _json_response({"ok": True})

    async def api_update_get(request):
        from . import updater
        loop = asyncio.get_running_loop()
        force = request.query.get("force") == "1"
        return _json_response(await loop.run_in_executor(None, lambda: updater.check(force=force)))

    async def api_update_post(request):
        from . import updater
        if state.busy:
            return _json_response({"ok": False, "log": "finish or stop the current turn first"}, 409)
        loop = asyncio.get_running_loop()
        ok, log = await loop.run_in_executor(None, updater.apply_update)
        if ok:
            session.save()
            # restart after this response is flushed; the page reconnects by itself
            loop.call_later(1.0, updater.restart)
        return _json_response({"ok": ok, "log": log, "restarting": ok})

    async def api_config_get(request):
        loop = asyncio.get_running_loop()   # options may query the model server
        rows = await loop.run_in_executor(None, lambda: settings_edit.rows(session, with_options=True))
        return _json_response({"rows": rows})

    async def api_config_set(request):
        data = await request.json()
        try:
            msg = settings_edit.apply(session, data.get("key", ""), data.get("value"),
                                      data.get("scope", "session"))
        except (KeyError, ValueError, AssertionError, SystemExit) as exc:
            return _json_response({"error": str(exc)}, 400)
        state.broadcast({"t": "status", "status": status_state(session)})
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(None, lambda: settings_edit.rows(session, with_options=True))
        return _json_response({"ok": True, "message": msg, "rows": rows})

    async def api_approve(request):
        data = await request.json()
        holder = state.approvals.get(int(data.get("id", 0)))
        if not holder:
            return _json_response({"error": "no pending approval"}, 404)
        holder["verdict"] = data.get("verdict", "no")
        holder["event"].set()
        return _json_response({"ok": True})

    async def api_clear(request):
        session.messages = []
        session.last_usage = {}
        state.broadcast({"t": "history", "items": [], "status": status_state(session)})
        return _json_response({"ok": True})

    async def api_compact(request):
        def run():
            session.compact(make_provider(session.model_config, session.settings),
                            manual=True)
        threading.Thread(target=run, daemon=True).start()
        return _json_response({"ok": True})

    # -- OpenAI-compatible endpoint (read-only tools; OpenWebUI points here)

    async def v1_chat(request):
        data = await request.json()
        incoming = [m for m in data.get("messages", []) if m.get("role") != "system"]
        ephemeral = Session(session.model_config, session.settings, session.cwd,
                            session.tools, session.system_prompt)
        ephemeral.bind_tool_ctx(session.tool_ctx)
        ephemeral.messages = [dict(m) for m in incoming]
        tool_defs = [{"name": t.name, "description": t.description,
                      "parameters": t.parameters} for t in session.tools]

        def answer_text(max_steps: int = 6) -> str:
            provider = make_provider(ephemeral.model_config, ephemeral.settings)
            for _ in range(max_steps):
                out = ephemeral._stream_assistant(provider, tool_defs)
                ephemeral.messages.append(out)
                calls = out.get("tool_calls") or []
                if not calls:
                    return out.get("content", "")
                if ephemeral._provider_error:
                    break
                for call in calls:
                    tool = next((t for t in session.tools if t.name == call["name"]), None)
                    if tool is None or not (tool.readonly or getattr(tool, "always_ok", False)):
                        ephemeral.messages.append({
                            "role": "tool", "tool_call_id": call["id"], "is_error": True,
                            "content": f"tool '{call.get('name')}' unavailable over the API "
                                       "(read-only tools only); answer from what you have"})
                        continue
                    result: ToolResult = tool.run(call["arguments"], session.tool_ctx)
                    entry = {"role": "tool", "tool_call_id": call["id"],
                             "content": _shorten(result.text, 6000)}
                    if result.is_error:
                        entry["is_error"] = True
                    ephemeral.messages.append(entry)
            return ephemeral.messages[-1].get("content", "")

        loop = asyncio.get_running_loop()
        if data.get("stream"):
            response = web.StreamResponse(
                headers={"content-type": "text/event-stream",
                         "cache-control": "no-cache"})
            await response.prepare(request)
            text = await loop.run_in_executor(None, answer_text)
            for chunk in _chunks(text):
                payload = json.dumps({"id": "hexiel", "object": "chat.completion.chunk",
                                      "model": session.model_config.model,
                                      "choices": [{"index": 0, "delta": {"content": chunk}}]})
                await response.write(f"data: {payload}\n\n".encode())
            await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
            return response
        content = await loop.run_in_executor(None, answer_text)
        return _json_response({
            "id": "hexiel-" + str(int(time.time())), "object": "chat.completion",
            "model": session.model_config.model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}]})

    def _chunks(text: str, size: int = 64):
        for i in range(0, len(text), size):
            yield text[i:i + size]

    app.router.add_get("/", index)
    app.router.add_get("/static/{name}", static)
    app.router.add_get("/ws", websocket)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/paste-image", api_paste_image)
    app.router.add_post("/api/model", api_model)
    app.router.add_post("/api/settings", api_settings)
    app.router.add_get("/api/commands", api_commands)
    app.router.add_post("/api/heal/decide", api_heal_decide)
    app.router.add_get("/api/models/live", api_models_live)
    app.router.add_post("/api/models/select", api_models_select)
    app.router.add_get("/api/update", api_update_get)
    app.router.add_post("/api/update", api_update_post)
    app.router.add_get("/api/config", api_config_get)
    app.router.add_post("/api/config", api_config_set)
    app.router.add_post("/api/approve", api_approve)
    app.router.add_post("/api/clear", api_clear)
    app.router.add_post("/api/compact", api_compact)
    app.router.add_post("/v1/chat/completions", v1_chat)
    return app


def serve(session: Session, host: str = "127.0.0.1", port: int = 8777) -> None:
    banner(host, port, session)
    try:
        asyncio.run(_serve_and_wait(host, port, session))
    except KeyboardInterrupt:
        pass


def banner(host: str, port: int, session: Session) -> None:
    if host not in ("127.0.0.1", "localhost"):
        print(f"WARNING: binding {host} — anyone on the network can drive the agent, "
              "including running shell commands. Local-only is the safe default.")
    print()
    print("  ⚡ hexiel web UI")
    print(f"     http://{host}:{port}/")
    print(f"  model: {session.model_config.model}")
    print(f"  OpenAI-compatible API: POST http://{host}:{port}/v1/chat/completions")
    print("  Ctrl+C to stop")
    print()


async def _serve_and_wait(host: str, port: int, session: Session) -> None:
    runner = web.AppRunner(make_app(session))
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    try:
        await asyncio.Event().wait()  # serve until interrupted
    finally:
        await runner.cleanup()