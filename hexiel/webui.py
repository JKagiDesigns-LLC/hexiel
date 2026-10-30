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

from . import media as media_mod
from . import memory as memory_mod
from . import settings_edit
from .config import approve_mode
from . import skills as skills_mod
from . import timers as timers_mod
from . import todo as todo_mod
from .config import load_config, home_data_dir
from .llm import make_provider
from .session import STEERING_NOTE, Session
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


def media_items(paths) -> list[dict]:
    """Attachment cards for the page: id to fetch it by, plus what to caption it with."""
    out = []
    for path in paths:
        info = media_mod.describe(path)
        if info:
            out.append({k: info[k] for k in ("kind", "name", "id", "w", "h", "pages") if k in info})
    return out


def status_state(session: Session) -> dict:
    tokens_now = session.tokens_now()
    window = session.model_config.context_window
    return {
        "model": session.model_config.name,
        "model_id": getattr(session, "active_model", "") or session.model_config.model,
        "context": {"tokens": tokens_now, "window": window,
                    "pct": min(100, 100 * tokens_now // max(window, 1)),
                    "compact_at_pct": int(100 * session.settings.compact_threshold)},
        "auto_approve": session.auto_approve,
        "auto_mode": approve_mode(session.auto_approve, session.guard_commits),
        "busy": bool(session.busy_probe()) if getattr(session, "busy_probe", None) else False,
        "todo": todo_mod.summary_line(session.cwd) or None,
        # end times, not "time left": every view counts down on its own clock and stays in step
        "timers": [{"id": t["id"], "label": t["label"], "started": t["started"], "ends": t["ends"]}
                   for t in timers_mod.live(session)],
        "now": time.time(),
    }


class WebState:
    """Server-side canonical chat view, shared by all browser tabs."""

    def __init__(self, session: Session, driver=None) -> None:
        self.session = session
        self.driver = driver       # a terminal UI that owns the chat: it runs the turns, we only mirror it
        self.clients: dict[web.WebSocketResponse, asyncio.AbstractEventLoop] = {}
        self.approvals: dict[int, dict] = {}   # id -> {"event": Event, "verdict": str}
        self.lock = threading.Lock()
        self.busy = False
        self.queue: "queue.Queue[bytes]" = __import__("queue").Queue()
        # What a view that joins mid-turn missed: the live frames since the turn's user message (notes, plan
        # steps, helper tool calls and the reply being written are not in session.messages until the turn ends).
        self.turn_base: int | None = None
        self.replay: list[dict] = []
        self.replay_lock = threading.Lock()
        if driver is None:
            session.on_event = self.on_event
            session.on_approve = self.ask_approval
        else:
            session.add_mirror(self)
            if self.is_busy():     # remote control turned on mid-turn: a view catches up from the request at work
                users = [i for i, m in enumerate(session.messages) if m.get("role") == "user"]
                self.turn_base = users[-1] if users else None
        # Every status frame says whether a turn is running, so a tab that joins (or reconnects) mid-turn shows Stop.
        session.busy_probe = self.is_busy

    def has_viewers(self) -> bool:
        return bool(self.clients)

    def cancel_approvals(self) -> None:
        for holder in list(self.approvals.values()):
            holder["verdict"] = "no"
            holder["event"].set()

    def is_busy(self) -> bool:
        return self.driver.busy() if self.driver is not None else self.busy

    def broadcast(self, message: dict) -> None:
        # called from worker threads: serialize onto the queue; the drain task
        # (started per app) delivers to every connected tab
        self._remember(message)
        try:
            self.queue.put(json.dumps(message))
        except Exception:
            pass

    HISTORY_MAX = 400                 # items sent per refresh: a long chat over the relay (a phone) stays quick

    def trim(self, items: list[dict]) -> list[dict]:
        if len(items) <= self.HISTORY_MAX:
            return items
        cut = len(items) - self.HISTORY_MAX
        return [{"t": "note", "text": f"— {cut} earlier items not shown (the full chat is saved on the machine) —"},
                *items[cut:]]

    REPLAYED = ("text", "note", "tool", "tool_done", "graphic", "incident")
    REPLAY_MAX = 800

    def _remember(self, message: dict) -> None:
        kind = message.get("t")
        with self.replay_lock:
            if self.turn_base is None or kind not in self.REPLAYED:
                return
            if kind == "text" and self.replay and self.replay[-1].get("t") == "text":
                self.replay[-1] = {"t": "text", "delta": self.replay[-1]["delta"] + message["delta"]}
                return
            self.replay.append(message)
            if len(self.replay) > self.REPLAY_MAX:
                del self.replay[:len(self.replay) - self.REPLAY_MAX]

    def catch_up(self) -> dict:
        """The first frame a view gets: the saved conversation, plus (mid-turn) what happened since it began and
        any approval still waiting — so a tab opened or woken up later sees exactly what a tab that never left sees."""
        msgs = list(self.session.messages)
        with self.replay_lock:
            base, frames = self.turn_base, list(self.replay)
        midturn = self.is_busy() and base is not None and base < len(msgs)
        return {"t": "history",
                "items": self.trim(self.render_history(msgs[:base + 1] if midturn else msgs)),
                "replay": frames if midturn else [],
                "approvals": [h["frame"] for h in list(self.approvals.values()) if "frame" in h],
                "status": status_state(self.session)}

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
        elif event in ("model_active", "timers"):
            self.broadcast({"t": "status", "status": status_state(self.session)})
        elif event == "note":
            self.broadcast({"t": "note", "text": str(payload[0])})
        elif event == "model_unreachable":
            self.broadcast({"t": "note", "text": f"⚠ model server not responding — retrying in {int(payload[0])}s "
                                                f"({payload[1]})"})
        elif event == "fallback":
            self.broadcast({"t": "status", "status": status_state(self.session)})
            self.broadcast({"t": "note", "text": f"↪ {payload[0]} is limited — continuing on {payload[1]}"})
        elif event == "turn_start":
            with self.replay_lock:
                self.turn_base, self.replay = len(self.session.messages) - 1, []
            self.broadcast({"t": "status", "status": status_state(self.session)})     # e.g. started in the terminal
        elif event == "turn_end":
            with self.replay_lock:
                self.turn_base, self.replay = None, []
            error = payload[0].get("error") if payload and isinstance(payload[0], dict) else None
            # Stop is not a failure: it was reported as error "stopped by user", which the views showed in red
            stopped = bool(_kw.get("aborted"))
            self.broadcast({"t": "turn_end", "error": None if stopped else error, "stopped": stopped,
                            "messages": self.trim(self.render_history())})
        elif event == "tool_call":
            self.broadcast({"t": "tool", **short_call(payload[0])})
        elif event == "tool_result":
            call, text, is_error = payload[0], payload[1], payload[2]
            elapsed = payload[3] if len(payload) > 3 else 0.0
            done = {"t": "tool_done", "id": call.get("id", ""), "ok": not is_error,
                    "elapsed": round(elapsed, 1), "preview": _shorten(text)}
            if call.get("name") == "view_image" and not is_error:
                done["media"] = media_items([self._media_path(call)])
            self.broadcast(done)
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
        elif event == "graphic":
            self.broadcast({"t": "graphic", "card": payload[0]})

    def ask_approval(self, tool_name: str, args) -> str:
        request_id = time.time_ns() % 10 ** 9
        holder = {"event": threading.Event(), "verdict": "no"}
        self.approvals[request_id] = holder
        titles = {"delegate": "plan this with the strong model?", "delegate-plan": "run this plan?"}
        shown = (str(args["text"])[:2400] if tool_name in titles and isinstance(args, dict) and "text" in args
                 else json.dumps(args, indent=2)[:1400])
        holder["frame"] = {"t": "approval", "id": request_id, "tool": titles.get(tool_name, tool_name),
                           "args": shown}
        self.broadcast(holder["frame"])
        # Like the terminal, wait for an answer however long you are away (a reconnecting browser is shown the
        # prompt again); only Stop gives up. A silent timeout used to deny the step while nobody was looking.
        while not holder["event"].wait(1):
            if self.session._stop.is_set():
                break
        self.approvals.pop(request_id, None)
        verdict = holder["verdict"] if holder["verdict"] in ("yes", "always", "no") else "no"
        self.broadcast({"t": "approval_done", "id": request_id, "verdict": verdict})
        return verdict

    # -- history rendering

    def _media_path(self, call: dict) -> str:
        if call.get("name") != "view_image":
            return ""
        raw = Path(str(call.get("arguments", {}).get("path", ""))).expanduser()
        return str(raw if raw.is_absolute() else Path(self.session.cwd) / raw)

    def render_history(self, messages: list[dict] | None = None) -> list[dict]:
        items: list[dict] = []
        pending: dict[str, dict] = {}
        for msg in (self.session.messages if messages is None else messages):
            role = msg["role"]
            if role == "user":
                body = msg.get("content", "")
                if body.startswith("<auto-compaction>"):
                    items.append({"t": "note", "text": "— earlier history auto-compacted —"})
                else:
                    item = {"t": "user", "text": body.removeprefix(STEERING_NOTE)}   # sent mid-turn
                    shown = media_items(a.get("path", "") for a in msg.get("attachments", []))
                    if shown:
                        item["media"] = shown
                    items.append(item)
            elif role == "assistant":
                # the model's text precedes the tool calls it then makes
                text = msg.get("content", "")
                if text.strip():
                    items.append({"t": "assistant", "text": text})
                for call in msg.get("tool_calls", []):
                    pending[call["id"]] = {"id": call["id"], "t": "tool", **short_call(call),
                                       "path": self._media_path(call)}
                    items.append(pending[call["id"]])
            elif role == "graphic":
                if msg.get("graphics"):
                    items.append({"t": "graphic", "card": msg["graphics"]})
            elif role == "tool":
                entry = pending.get(msg.get("tool_call_id", ""))
                if entry is not None:
                    entry["ok"] = not msg.get("is_error")
                    entry["preview"] = _shorten(msg.get("content", ""))
                    if entry["name"] == "view_image" and not msg.get("is_error"):
                        entry["media"] = media_items([entry.get("path", "")])
        for item in items:
            item.pop("path", None)             # a local path is only for finding the file, not for the page
        return items


def queued_note(text: str) -> dict:
    """Said when a message arrives mid-turn. It quotes the message, so a view that reconnects before the model
    reads it (and it joins the saved chat) still shows what was sent."""
    shown = " ".join(text.split())
    shown = shown if len(shown) <= 120 else shown[:117] + "…"
    return {"t": "note", "text": f"↳ sent “{shown}” — hexiel will read this before its next step "
                                 "(Stop ends the current work)"}


def _json_response(data: dict, status: int = 200) -> web.Response:
    return web.Response(text=json.dumps(data), content_type="application/json",
                        status=status)


def make_app(session: Session, driver=None) -> web.Application:
    state = WebState(session, driver)
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
            await ws.send_str(json.dumps(state.catch_up()))
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
            healer.guard_commits = session.guard_commits
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
        if cmd == "auto":
            words = {"off": "off", "commits": "except-commits", "except-commits": "except-commits",
                     "safe": "except-commits", "all": "all", "everything": "all", "on": "all"}
            if arg and arg.lower() not in words:
                return "usage: /auto (cycles) or /auto off | commits | all"
            now = approve_mode(session.auto_approve, session.guard_commits)
            new = words[arg.lower()] if arg else {"off": "except-commits", "except-commits": "all", "all": "off"}[now]
            settings_edit.apply(session, "settings.auto_approve", new, "session")
            return {"off": "auto-approve OFF — hexiel asks before edits and commands",
                    "except-commits": "auto-approve ON, except commits — git/svn commits and pushes still ask first",
                    "all": "⚠ auto-approve EVERYTHING — nothing asks, including commits, pushes and deletes"}[new]
        if cmd == "remote-control":
            return remote_control_command(arg)
        if cmd in ("bug", "feature"):
            from . import heal as heal_mod, reporter
            return reporter.file_report(cmd, arg, heal_mod.unhealed(session.cwd, 3))
        if cmd == "bugs":
            from . import reporter
            return reporter.my_reports_text()
        if cmd == "undo":
            restored = session.undo()
            return ("↶ " + "\n".join(restored)) if restored else "nothing to undo"
        if cmd in ("delegate", "solo", "cloud"):
            return "prompt:" + text           # the session parses the prefix
        if cmd == "plan":
            session.plan_mode = not session.plan_mode
            return "📝 plan mode ON — investigate and plan, change nothing" if session.plan_mode else "plan mode OFF"
        if cmd == "spend":
            from . import spend as spend_mod
            return spend_mod.command(session.settings, arg)
        if cmd == "usage":
            t = session.usage_totals
            from . import digest as digest_mod
            from . import spend as spend_mod
            return "\n".join([f"{t['requests']} request(s) · input {t['prompt_tokens']:,} ({t['cached_tokens']:,} cached) · "
                              f"output {t['completion_tokens']:,} tokens",
                              *([digest_mod.summary_line(session)] if digest_mod.summary_line(session) else []),
                              *spend_mod.summary_lines(session.settings)])
        if cmd == "toolmodels":
            from . import digest as digest_mod
            if arg.lower() == "test":
                threading.Thread(target=lambda: state.broadcast({"t": "note", "text": digest_mod.status_text(session, True)}), daemon=True).start()
                return "timing each tool model…"
            return digest_mod.status_text(session)
        if cmd == "permissions":
            r = session.rules
            return "\n".join(f"{k}: " + (", ".join(getattr(r, k)) or "—") for k in ("deny", "ask", "allow"))
        if cmd == "jobs":
            return "\n".join(jobs_mod.describe(j) for j in jobs_mod.load()) or "no scheduled jobs"
        if cmd == "inbox":
            box = jobs_mod.inbox_dir()
            files = sorted(box.glob("*.md"), reverse=True)[:3] if box.exists() else []
            return "\n\n".join(f.read_text()[:3000] for f in files) or "the inbox is empty"
        if cmd == "knowledge":
            from . import knowledge, toolsets
            if arg != "sync":
                return knowledge.status() + "\n(add docs to the folder, then /knowledge sync)"

            def sync_knowledge() -> None:
                lines: list[str] = []
                try:
                    stats = knowledge.sync(say=lines.append)
                    if knowledge.has_content() and "knowledge" not in session.loaded_toolsets:
                        toolsets.load(session, "knowledge")
                    lines.append(f"{stats['added']} added, {stats['changed']} changed, "
                                 f"{stats['removed']} removed, {stats['embedded']} embedded")
                    lines.append(knowledge.status())
                except Exception as exc:  # noqa: BLE001 - report, don't crash the server
                    lines.append(f"sync failed: {exc}")
                state.broadcast({"t": "note", "text": "\n".join(lines[-8:])})
            threading.Thread(target=sync_knowledge, daemon=True).start()
            return f"syncing {knowledge.knowledge_dir()} …"
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

    def remote_control_command(arg: str) -> str:
        """/remote-control [off|status] typed in the browser (the local web UI, not through the relay)."""
        from . import remote_control
        if arg.lower() in ("off", "stop"):
            return remote_control.stop(session)
        if arg.lower() == "status":
            return remote_control.status_text(session)
        if remote_control.active(session):
            return remote_control.status_text(session)
        if not remote_control.signed_in():
            state.broadcast({"t": "remote_login"})
            return ("Hexiel Remote needs an account: sign in below, or sign up at "
                    "https://remote.hexiel.tech first, then type /remote-control again.")
        note = remote_control.start(session, local_base=session.web_base, say=lambda m: state.broadcast({"t": "note", "text": m}))
        return note

    async def api_remote_login(request):
        """Local only (path_allowed keeps /api/remote/ off the tunnel): sign this machine in to Hexiel Remote."""
        from . import remote_control
        data = await request.json()
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda: remote_control.sign_in(
                str(data.get("email", "")).strip(), str(data.get("password", ""))))
        except (RuntimeError, OSError) as exc:
            return _json_response({"error": str(exc)}, 400)
        note = await loop.run_in_executor(None, lambda: remote_control.start(
            session, local_base=session.web_base, say=lambda m: state.broadcast({"t": "note", "text": m})))
        return _json_response({"ok": True, "message": note})

    def handle_ws_message(data: dict) -> None:
        if data.get("t") == "send":
            text = str(data.get("text", "")).strip()
            if text.startswith("/") and not state.is_busy():
                reply = web_command(text)
                if reply and not reply.startswith("prompt:"):
                    state.broadcast({"t": "note", "text": reply})
                    state.broadcast({"t": "status", "status": status_state(session)})
                    return
                if reply:
                    text = reply[len("prompt:"):]
            if state.driver is not None:
                if text:
                    if state.driver.busy():         # the terminal queues it for the model's next step
                        state.broadcast(queued_note(text))
                    state.driver.submit(text)       # the terminal UI shows it and runs the turn
                return
            with state.lock:
                if not text:
                    return
                if state.busy:               # typed mid-turn: the model reads it before its next step
                    session.queue_message(text)
                    state.broadcast(queued_note(text))
                    return
                state.busy = True
                def run(first):
                    nxt = first
                    while nxt:
                        try:
                            session.run_turn(nxt)
                        finally:
                            left = session.take_pending()      # arrived as the turn was ending
                            nxt = "\n\n".join(left) if left else None
                            if not nxt:
                                with state.lock:
                                    state.busy = False
                            state.broadcast({"t": "status", "status": status_state(session)})
                threading.Thread(target=run, args=(text,), daemon=True).start()
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
        """A pasted or dropped image or PDF: saved under .hexiel/pastes, returned with what the page needs to show it."""
        from .clipboard import save_image
        mime = request.content_type
        if mime not in ("image/png", "image/jpeg", "image/webp", "image/gif", "application/pdf"):
            return _json_response({"error": "unsupported file type (png, jpeg, webp, gif or pdf)"}, status=400)
        data = await request.read()
        if not data or len(data) > 20 * 1024 * 1024:
            return _json_response({"error": "empty or over 20MB"}, status=400)
        if mime == "application/pdf" and not data.startswith(b"%PDF"):
            return _json_response({"error": "not a PDF"}, status=400)
        path = save_image(data, mime, session.cwd, request.headers.get("x-filename", ""))
        info = media_mod.describe(path) or {}
        return _json_response({"path": path, **{k: info[k] for k in ("kind", "name", "id", "w", "h", "pages") if k in info}})

    async def api_media(request):
        """Serve a file that surfaced in the conversation (see media.py) — nothing else."""
        path = media_mod.resolve(request.match_info["id"])
        if path is None:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Type": media_mod.MIME[path.suffix.lower()],
                                               "Content-Disposition": f'inline; filename="{path.name}"',
                                               "X-Content-Type-Options": "nosniff",
                                               "Cache-Control": "private, max-age=3600"})

    sessions_cache = {"at": 0.0, "rows": []}

    def recent_sessions() -> list[dict]:
        """This project's latest saved sessions. Reading them is the costly part of /api/state (the page polls it),
        so the listing is reused for 30s."""
        if time.time() - sessions_cache["at"] < 30:
            return sessions_cache["rows"]
        rows: list[dict] = []
        try:
            paths = sorted((home_data_dir() / "sessions").glob("*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:200]
            for p in paths:
                if len(rows) >= 12:
                    break
                try:
                    raw = json.loads(p.read_text())
                    if raw.get("cwd") != session.cwd:      # this project's sessions only
                        continue
                    preview = next((m.get("content", "")[:60] for m in raw.get("messages", [])
                                    if m.get("role") == "user"
                                    and not m.get("content", "").startswith("<auto-compaction>")), "")
                    rows.append({"id": raw.get("id", p.stem), "created": raw.get("created"),
                                 "model": raw.get("model"), "preview": preview})
                except (OSError, json.JSONDecodeError, KeyError):
                    continue
        except OSError:
            pass
        sessions_cache.update(at=time.time(), rows=rows)
        return rows

    async def api_state(request):
        cfg = load_config()
        chain = detect_toolchain(session.cwd)
        sessions = recent_sessions()
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
    app.router.add_get("/api/media/{id}", api_media)
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
    app.router.add_post("/api/remote/login", api_remote_login)
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
    session.web_base = f"http://127.0.0.1:{port}"       # /remote-control tunnels this page
    site = web.TCPSite(runner, host, port)
    await site.start()
    try:
        await asyncio.Event().wait()  # serve until interrupted
    finally:
        await runner.cleanup()