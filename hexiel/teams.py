"""Agent teams: the `team` tool starts several named teammates at once (each a subagent, see Session.run_subagent)
that can talk to each other while they work. A teammate sends with `team_msg`; the message reaches the other
teammate before its next step (the same path as a user typing mid-turn), so a reviewer can ask the implementer a
question, a researcher can hand findings to the writer, and nobody has to wait for the whole team to finish.

The lead (the main conversation) gets every teammate's final answer plus the message log. /team shows the board:
who is working, their last step, and the messages so far."""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

MAX_MEMBERS = 6

TEAM_NOTE = """

You are "{name}" on a team of agents working in parallel: {roster}. Your part: {role}
Talk to teammates with team_msg (send to one by name, or to "all"). Their messages reach you automatically, marked
[team message from NAME] — answer questions you are asked. Share what another teammate needs as soon as you have it
rather than at the end. When your part is done, give your final answer."""


class Board:
    """Who is on the team, what each is doing, and every message sent. Thread-safe."""

    def __init__(self, members: list[dict], on_note=None):
        self.lock = threading.Lock()
        self.started = time.time()
        self.members = {m["name"]: {"role": m.get("role", ""), "status": "starting", "steps": 0, "last": "",
                                    "session": None, "answer": "", "held": []} for m in members}
        self.log: list[tuple[float, str, str, str]] = []      # (time, from, to, text)
        self.on_note = on_note or (lambda text: None)

    def attach(self, name: str, session) -> None:
        with self.lock:
            m = self.members[name]
            m["session"], m["status"] = session, "working"
            held, m["held"] = m["held"], []
        for text in held:                                       # sent before this teammate had started
            session.queue_message(text)

    def step(self, name: str, tool: str) -> None:
        with self.lock:
            m = self.members[name]
            m["steps"] += 1
            m["last"] = tool

    def finish(self, name: str, answer: str, failed: bool = False) -> None:
        with self.lock:
            m = self.members[name]
            m["status"], m["answer"], m["session"] = ("failed" if failed else "done"), answer, None

    def post(self, sender: str, to: str, text: str) -> str:
        text = text.strip()
        if not text:
            return "error: the message is empty"
        targets = [n for n in self.members if n != sender] if to.lower() in ("all", "*", "everyone", "team") \
            else [self._resolve(to)]
        if targets == [None]:
            return f"error: no teammate called {to!r} — the team is {', '.join(self.members)}"
        note = f"[team message from {sender}] {text}"
        delivered, late = [], []
        with self.lock:
            self.log.append((time.time(), sender, to if len(targets) > 1 else targets[0], text))
            for t in targets:
                m = self.members[t]
                if m["status"] in ("done", "failed"):
                    late.append(t)
                elif m["session"] is None:
                    m["held"].append(note)
                    delivered.append(t)
                else:
                    m["session"].queue_message(note)
                    delivered.append(t)
        self.on_note(f"↳ team {sender} → {', '.join(targets)}: {text[:100]}")
        out = f"sent to {', '.join(delivered)}" if delivered else ""
        if late:
            out += ("; " if out else "") + f"{', '.join(late)} already finished (the lead will see this message)"
        return out

    def wait(self, name: str, seconds: float) -> str:
        """Block (up to `seconds`) until a message for `name` arrives or every other teammate has finished."""
        deadline = time.time() + max(1.0, min(float(seconds or 60), 600.0))
        while time.time() < deadline:
            with self.lock:
                me = self.members[name]["session"]
                others_busy = any(m["status"] in ("starting", "working") for n, m in self.members.items() if n != name)
            if me is not None and (me.has_pending() or me._stop.is_set()):
                return "a message arrived — it is shown next"
            if not others_busy:
                return "no message: every other teammate has finished (their answers go to the lead)"
            time.sleep(0.2)
        return f"no message within {seconds:.0f}s — carry on"

    def _resolve(self, to: str) -> str | None:
        if to in self.members:
            return to
        low = {n.lower(): n for n in self.members}
        return low.get(to.lower().lstrip("@")) or next((n for k, n in low.items() if k.startswith(to.lower())), None)

    def render(self) -> str:
        lines = [f"team · {len(self.members)} member(s) · {time.time() - self.started:.0f}s"]
        mark = {"starting": "…", "working": "▶", "done": "✓", "failed": "✗"}
        for name, m in self.members.items():
            last = f" · last: {m['last']}" if m["last"] else ""
            lines.append(f"  {mark.get(m['status'], '?')} {name:<12} {m['status']:<8} {m['steps']} step(s){last}"
                         + (f"  — {m['role'][:60]}" if m["role"] else ""))
        if self.log:
            lines.append("messages:")
            for ts, frm, to, text in self.log[-20:]:
                lines.append(f"  +{ts - self.started:4.0f}s {frm} → {to}: {text[:140]}")
        return "\n".join(lines)


def _clean_name(raw: str, taken: set) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", str(raw or "").strip()).strip("-")[:20] or "mate"
    base, n = name, 2
    while name in taken:
        name, n = f"{base}{n}", n + 1
    return name


def tool_def(session):
    from .tools import ToolDef
    return ToolDef(
        "team",
        "Start a team of 2-6 named agents that work in parallel AND message each other while they work (unlike "
        "`task`, whose helpers cannot talk). Use it when the parts depend on each other: e.g. an implementer and a "
        "reviewer who asks it questions, a researcher feeding a writer, frontend and backend agreeing on an API. "
        "Each member gets its own context; mode=write members each work in their own git worktree. You get every "
        "member's final answer and the message log.",
        {"type": "object", "properties": {
            "members": {"type": "array", "description": "the teammates", "items": {"type": "object", "properties": {
                "name": {"type": "string", "description": "short name, e.g. backend, reviewer"},
                "role": {"type": "string", "description": "one line: this member's part"},
                "prompt": {"type": "string", "description": "the member's full task, with everything it needs"},
                "mode": {"type": "string", "enum": ["research", "write"]}}, "required": ["name", "prompt"]}},
            "goal": {"type": "string", "description": "the shared goal every member is told"}},
         "required": ["members"]},
        readonly=True, run=lambda args, ctx: run_team(session, args))


def _msg(board: Board, name: str, a: dict) -> str:
    out = []
    if str(a.get("text", "")).strip():
        out.append(board.post(name, str(a.get("to") or "all"), str(a["text"])))
    if a.get("wait"):
        out.append(board.wait(name, float(a["wait"])))
    return "; ".join(out) or "error: give text to send, and/or wait=seconds to wait for a message"


def run_team(lead, args: dict):
    from .tools import ToolDef, ToolResult, text_result
    raw = [m for m in (args.get("members") or []) if isinstance(m, dict) and str(m.get("prompt", "")).strip()]
    if len(raw) < 2:
        return ToolResult(text="error: a team needs at least two members, each with a prompt (use `task` for one)",
                          is_error=True)
    raw = raw[:MAX_MEMBERS]
    taken: set = set()
    members = []
    for m in raw:
        name = _clean_name(m.get("name"), taken)
        taken.add(name)
        members.append({**m, "name": name})
    board = Board(members, on_note=lambda text: lead.on_event("note", text))
    lead.team_board = board
    roster = ", ".join(f"{m['name']} ({m.get('role') or 'see prompt'})" for m in members)
    goal = str(args.get("goal") or "").strip()

    def one(m: dict):
        name = m["name"]
        msg_tool = ToolDef(
            "team_msg", "Send a message to a teammate by name, or to \"all\". It reaches them before their next "
            "step. Use it to ask questions, hand over findings, or agree on interfaces. wait=N (seconds) then "
            "waits for a reply (or any message) — with no text, it only waits.",
            {"type": "object", "properties": {"to": {"type": "string"}, "text": {"type": "string"},
                                              "wait": {"type": "number", "description": "seconds to wait for a reply"}}},
            readonly=True, run=lambda a, ctx: text_result(_msg(board, name, a)))
        prompt = (f"Team goal: {goal}\n\n" if goal else "") + m["prompt"] + TEAM_NOTE.format(
            name=name, roster=roster, role=m.get("role") or "as described above")

        def hook(helper):
            board.attach(name, helper)
            inner = helper.on_event

            def on_event(event, *p, **k):
                if event == "tool_call" and p:
                    board.step(name, p[0].get("name", ""))
                return inner(event, *p, **k)
            helper.on_event = on_event
        try:
            result = lead.run_subagent({"prompt": prompt, "description": f"team-{name}",
                                        "mode": m.get("mode") or "research"},
                                       extra_tools=[msg_tool], on_helper=hook)
        except Exception as exc:  # noqa: BLE001 - one member failing must not sink the team
            board.finish(name, f"error: {exc}", failed=True)
            return
        board.finish(name, result.text, failed=result.is_error)

    with ThreadPoolExecutor(max_workers=len(members), thread_name_prefix="hexiel-team") as pool:
        list(pool.map(one, members))
    if lead._stop.is_set():
        return ToolResult(text="error: cancelled by the user before the team finished\n\n" + board.render(),
                          is_error=True)
    parts = [board.render(), ""]
    for name, m in board.members.items():
        parts.append(f"## {name} ({m['status']})\n{m['answer'][:6000]}")
    return ToolResult(text="\n".join(parts), is_error=all(m["status"] == "failed" for m in board.members.values()))
