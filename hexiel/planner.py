"""Delegation: a strong model plans a big task, a cheaper (usually local) model does the work.

Flow (see /delegate, and the `auto_delegate` setting):

    triage    the request is sized up, cheaply (rules first, the local model only when unsure)
    brief     a read-only helper on the worker model gathers the relevant files and conventions
    plan      ONE call to the planner model turns request + brief into small, self-contained tasks
    approve   the user sees the plan (delegate_approval)
    execute   each task runs in a fresh, small context on the worker model, then its check runs;
              failures are retried, then escalated to the planner model once, then the plan stops
    wrap up   a summary built in code (not by a model) is added to the conversation

The plan is saved in .hexiel/plans/ and mirrored in .hexiel/todo.md so `/delegate resume` can continue.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import llm
from . import todo as todo_mod

MAX_TASKS = 12
REVIEW_DIFF_CHARS = 24000          # how much of the combined diff the reviewer is shown
REVIEW_FILE_CHARS = 6000           # ... and of any one file
MAX_REVIEW_ISSUES = 6
BRIEF_TOOL_CAP = 12
LEDGER_CHARS = 400
CHECK_TIMEOUT = 600

# ---------------------------------------------------------------- prompts

TRIAGE_SYSTEM = (
    "You size up requests made to a coding agent. Reply with ONLY a JSON object: "
    '{"scale": "small" or "large", "reason": "<six words>"}. '
    '"large" = touches several files or needs a careful plan or design before coding; '
    '"small" = a question, an explanation, or a focused change in one or two files.')

BRIEF_SYSTEM = (
    "You prepare a briefing for a senior engineer who cannot see the code. Using only the read-only tools "
    "(search, read, map), find what matters for the request and then write the briefing: "
    "the relevant files (exact paths, one line on each file's role), short key snippets with line numbers, "
    "project conventions, and the commands that build and test the project. Facts only, no plan. "
    f"Use at most {BRIEF_TOOL_CAP} tool calls and keep the briefing under 700 words.")

PLANNER_SYSTEM = (
    "You are the planning lead on a coding team. A cheaper model will carry out your plan ONE TASK AT A TIME; "
    "each task runs in a FRESH context where that model sees only the task text and a one-line summary of the "
    "earlier tasks. It cannot ask you questions. Reply with ONLY a JSON object:\n"
    '{"summary": "<one sentence>", "tasks": [{"id": 1, "title": "<short imperative>", '
    '"goal": "<exactly what to change and where, so no further decisions are needed>", '
    '"files": ["<exact path>", ...], "constraints": "<what must not change>", '
    '"check": "<one shell command that exits 0 when this task is done, or empty>", "depends_on": [<ids>]}]}\n'
    f"Rules: 2 to {MAX_TASKS} tasks (fewer is better); each task is small enough for one focused session; "
    "use exact file paths from the briefing; order tasks so each depends only on earlier ones; "
    "a check is a single command such as a test run, a build or a grep, never something that needs the user; "
    "no vague tasks like 'improve' or 'clean up'; do not add work the user did not ask for; "
    "never make a task whose only job is to run tests or verify — put that command in the `check` of the "
    "task it verifies, and the plan's last task may carry a final overall check.")

REVIEW_SYSTEM = (
    "You are a senior engineer reviewing a change that a cheaper model just made, step by step, from a plan you can see. "
    "You get the user's request, the plan, the result of each step's check and the combined diff. Look for REAL defects: "
    "requirements from the request that were missed or only half done, bugs, wrong edge cases, broken or missing tests, "
    "inconsistencies between files, security problems. Ignore style and taste. Reply with ONLY a JSON object:\n"
    '{"verdict": "ok" or "fix", "summary": "<one sentence>", "issues": [{"file": "<exact path>", '
    '"problem": "<what is wrong and how it shows>", "fix": "<the exact change to make>"}]}\n'
    "Use verdict ok with an empty list when the change is correct and complete. List at most 6 issues, most serious first; "
    "each fix must be specific enough for a junior engineer to apply without asking questions.")

TASK_PROMPT = (
    "You are carrying out ONE step of an approved plan. Do only this step.\n\n"
    "Overall goal: {summary}\n\n{ledger}"
    "## Step {n} of {total}: {title}\n{goal}\n"
    "{files}{constraints}{check}\n"
    "When you are done, reply with a short report (2-4 lines): what you changed and where. "
    "Do not start the next step.")

LARGE_HINTS = ("refactor", "migrate", "migration", "rewrite", "redesign", "architecture", "implement",
               "across", "every ", "all the ", "all of the", "entire", "whole ", "multiple files",
               "several files", "end to end", "end-to-end", "from scratch", "new feature", "integrate",
               "overhaul", "rework", "convert ", "support for", "set up", "design ")
SMALL_HINTS = ("typo", "rename", "what ", "why ", "how do", "how does", "explain", "show me", "list ",
               "where is", "one line", "quick", "status", "summar", "?")


class PlanError(Exception):
    pass


# ---------------------------------------------------------------- data

@dataclass
class Task:
    id: int
    title: str
    goal: str
    files: list = field(default_factory=list)
    constraints: str = ""
    check: str = ""
    depends_on: list = field(default_factory=list)
    status: str = "pending"          # pending | running | done | failed
    report: str = ""
    changed: list = field(default_factory=list)


@dataclass
class Plan:
    request: str
    summary: str
    tasks: list
    planner: str = ""
    worker: str = ""
    created: str = ""
    path: str = ""
    review: str = ""                 # "", or what the review found ("no problems", "2 issues found and fixed", ...)

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Plan":
        raw = json.loads(text)
        tasks = [Task(**t) for t in raw.pop("tasks")]
        return cls(tasks=tasks, **raw)


# ---------------------------------------------------------------- helpers shared with the session

def say(session, text: str) -> None:
    session.on_event("note", text)


def _config_profile(session, name: str):
    """A model profile by name; None when it isn't defined (callers fall back to another model)."""
    from .config import load_config
    if not name:
        return None
    try:
        return load_config().model(name)
    except (KeyError, SystemExit):
        return None


def worker_model(session):
    return _config_profile(session, session.settings.executor_profile) or session.model_config


def planner_model(session):
    st = session.settings
    return (_config_profile(session, st.planner_profile) or _config_profile(session, st.fallback_profile)
            or session.model_config)


def complete(session, mc, system: str, user: str, max_tokens: int = 4096) -> tuple[str, dict]:
    """One tool-less model call. Returns (text, usage). Stays responsive to Ctrl+C."""
    provider = llm.make_provider(mc, session.settings)
    stream = provider.stream([{"role": "user", "content": user}], system, [], model=mc.model,
                             max_output_tokens=max_tokens, extra_body=mc.extra_body,
                             timeout=session.settings.request_timeout)
    parts, usage = [], {}
    for event, *payload in session._interruptible(stream, provider):
        if event == "text":
            parts.append(payload[0])
        elif event == "done" and payload and payload[0]:
            usage = payload[0]
    session.record_usage(mc.model, usage)
    return "".join(parts).strip(), usage


def _json_from(text: str):
    """The first JSON object in a model reply (it may be fenced or have chatter around it)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in the reply")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            esc = (ch == "\\" and not esc)
            if ch == '"' and not esc:
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unterminated JSON object")


# ---------------------------------------------------------------- triage

def triage_score(prompt: str) -> tuple[int, list[str]]:
    """Cheap, rule-based size estimate: >= 3 looks large, <= 0 looks small, in between is unsure."""
    p = prompt.lower()
    words = len(p.split())
    score, why = 0, []
    if words >= 60:
        score += 2
        why.append("long request")
    elif words >= 30:
        score += 1
    hits = [h.strip() for h in LARGE_HINTS if h in p]
    if hits:
        score += min(len(hits), 3)
        why.append(hits[0])
    if len(re.findall(r"(?m)^\s*(?:\d+[.)]|[-*])\s+\S", prompt)) >= 3:
        score += 2
        why.append("a list of steps")
    if len(set(re.findall(r"[\w./-]+\.[a-z]{1,5}\b", prompt))) >= 3:
        score += 1
        why.append("several files named")
    small = [h for h in SMALL_HINTS if h in p]
    score -= min(len(small), 2)
    if words < 12:
        score -= 1
    return score, why


def triage(session, prompt: str) -> tuple[bool, str]:
    """(is it large, why). Rules decide the clear cases; the worker model only breaks ties."""
    score, why = triage_score(prompt)
    if score >= 3:
        return True, ", ".join(why) or "looks big"
    if score <= 0:
        return False, "small"
    try:
        text, _ = complete(session, worker_model(session), TRIAGE_SYSTEM, prompt[:4000], max_tokens=200)
        verdict = _json_from(text)
        return str(verdict.get("scale", "")).lower() == "large", str(verdict.get("reason", ""))[:80]
    except Exception:  # noqa: BLE001 - any failure means "treat it as a normal request"
        return False, "could not size it"


# ---------------------------------------------------------------- plan parsing

def parse_plan(text: str, request: str) -> Plan:
    try:
        raw = _json_from(text)
    except (ValueError, json.JSONDecodeError) as exc:
        raise PlanError(f"the reply was not valid JSON ({exc})") from exc
    tasks_raw = raw.get("tasks")
    if not isinstance(tasks_raw, list) or not tasks_raw:
        raise PlanError('the JSON needs a non-empty "tasks" list')
    if len(tasks_raw) > MAX_TASKS:
        raise PlanError(f"too many tasks ({len(tasks_raw)}); the limit is {MAX_TASKS} — merge small ones")
    tasks: list[Task] = []
    seen: set = set()
    for i, t in enumerate(tasks_raw, 1):
        if not isinstance(t, dict):
            raise PlanError(f"task {i} is not an object")
        title, goal = str(t.get("title", "")).strip(), str(t.get("goal", "")).strip()
        if not title or not goal:
            raise PlanError(f"task {i} needs a title and a goal")
        tid = t.get("id", i)
        tid = tid if isinstance(tid, int) else i
        if tid in seen:
            raise PlanError(f"duplicate task id {tid}")
        deps = [d for d in (t.get("depends_on") or []) if isinstance(d, int)]
        if any(d not in seen for d in deps):
            raise PlanError(f"task {tid} depends on a task that does not come before it")
        seen.add(tid)
        files = [str(f) for f in (t.get("files") or []) if str(f).strip()]
        tasks.append(Task(id=tid, title=title[:120], goal=goal[:2000], files=files,
                          constraints=str(t.get("constraints") or "")[:600],
                          check=str(t.get("check") or "").strip()[:500], depends_on=deps))
    return Plan(request=request, summary=str(raw.get("summary", "")).strip()[:300] or request[:200],
                tasks=tasks, created=datetime.datetime.now().isoformat(timespec="seconds"))


def format_plan(plan: Plan, planner: str, worker: str) -> str:
    lines = [f"{plan.summary}", f"planned by {planner} · carried out by {worker}", ""]
    for t in plan.tasks:
        lines.append(f"{t.id}. {t.title}")
        if t.files:
            lines.append(f"     files: {', '.join(t.files[:6])}")
        if t.check:
            lines.append(f"     check: {t.check}")
    return "\n".join(lines)


# ---------------------------------------------------------------- persistence (plan file + todo.md)

def _plans_dir(cwd: str) -> Path:
    return Path(cwd) / ".hexiel" / "plans"


def save_plan(session, plan: Plan) -> None:
    try:
        if not plan.path:
            d = _plans_dir(session.cwd)
            d.mkdir(parents=True, exist_ok=True)
            plan.path = str(d / (datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ".json"))
        Path(plan.path).write_text(plan.to_json())
    except OSError:
        pass


def latest_unfinished(cwd: str) -> Plan | None:
    d = _plans_dir(cwd)
    if not d.is_dir():
        return None
    for path in sorted(d.glob("*.json"), reverse=True):
        try:
            plan = Plan.from_json(path.read_text())
        except (OSError, ValueError, TypeError, KeyError):
            continue
        plan.path = str(path)
        if any(t.status != "done" for t in plan.tasks):
            return plan
        return None          # the newest plan is finished: nothing to resume
    return None


_MARK = {"pending": " ", "running": "~", "done": "x", "failed": " "}


def sync_todo(session, plan: Plan, left_off: str = "") -> None:
    try:
        items = [todo_mod.TodoItem(state=_MARK[t.status], text=f"{t.id}. {t.title}") for t in plan.tasks]
        todo_mod._write(session.cwd, f"delegated: {plan.summary[:80]}", left_off, items)
    except Exception:  # noqa: BLE001 - bookkeeping must never break the run
        pass


# ---------------------------------------------------------------- briefing

def make_helper(session, mc, system: str, tools: list):
    """A fresh Session on `mc` that shares this session's tool context, rules and hooks."""
    from .session import Session
    settings = dataclasses.replace(session.settings, auto_delegate="off")
    helper = Session(mc, settings, session.cwd, tools, system)
    if any(t.name == "consult" for t in tools):            # the helper asks about ITS steps, not the parent's
        from . import consult
        helper.tools = [consult.tool_def(helper) if t.name == "consult" else t for t in tools]
    helper.bind_tool_ctx(session.tool_ctx)
    helper.hooks = session.hooks
    helper.rules = session.rules
    helper.auto_approve = session.auto_approve
    helper.guard_commits = session.guard_commits
    helper.save = lambda: None
    return helper


def _link_stop(session, helper):
    """run_turn() clears a session's stop flag, so mirror the parent's flag onto the helper with a watcher."""
    done = threading.Event()

    def watch():
        while not done.wait(0.2):
            if session._stop.is_set():
                helper.stop()

    threading.Thread(target=watch, daemon=True, name="hexiel-stop-link").start()
    return done


def build_brief(session, request: str) -> str:
    mc = worker_model(session)
    session.set_active_model(mc, "gathering context")
    tools = [t for t in session.tools if t.readonly and t.name != "task"]
    helper = make_helper(session, mc, BRIEF_SYSTEM, tools)
    calls = {"n": 0}

    def forward(event, *p, **k):
        if event == "tool_call":
            calls["n"] += 1
            session.on_event("subagent", "briefing", p[0].get("name", ""))
            if calls["n"] > BRIEF_TOOL_CAP + 2:
                helper.stop()

    helper.on_event = forward
    helper.on_approve = lambda *a: "no"
    link = _link_stop(session, helper)
    try:
        helper.run_turn(f"Prepare the briefing for this request:\n\n{request[:6000]}")
        brief = _last_text(helper)
        if not brief and not session._stop.is_set():       # it ran out of tool calls: ask for the write-up now
            helper.run_turn("Write the briefing now with what you have found.")
            brief = _last_text(helper)
    finally:
        link.set()
        session.merge_usage(helper)
    return brief[:6000]


def _last_text(helper) -> str:
    return next((m.get("content", "") for m in reversed(helper.messages)
                 if m.get("role") == "assistant" and m.get("content")), "").strip()


# ---------------------------------------------------------------- planning

def make_plan(session, request: str, brief: str) -> Plan:
    """One call to the planner model; falls back to the worker model if the planner is unreachable."""
    pmc, wmc = planner_model(session), worker_model(session)
    user = f"## Request\n{request[:6000]}\n\n## Briefing\n{brief or '(none: explore as the tasks require)'}"
    last_error = ""
    for mc in ([pmc] if pmc.name == wmc.name else [pmc, wmc]):
        attempt_user = user
        session.set_active_model(mc, "planning")
        for attempt in (1, 2):
            try:
                text, usage = complete(session, mc, PLANNER_SYSTEM, attempt_user, max_tokens=4096)
            except PlanError:
                raise
            except Exception as exc:  # noqa: BLE001 - provider down, key missing, limit hit ...
                last_error = f"{mc.name}: {str(exc)[:160] or type(exc).__name__}"
                say(session, f"⚠ the planner model could not be reached ({last_error})"
                    + (f" — planning with {wmc.name} instead" if mc is pmc and pmc.name != wmc.name else ""))
                break
            if session._stop.is_set():
                raise PlanError("cancelled")
            try:
                plan = parse_plan(text, request)
            except PlanError as exc:
                last_error = str(exc)
                attempt_user = (f"{user}\n\n## Your previous reply was rejected\n{exc}.\n"
                                "Reply again with ONLY the corrected JSON object.")
                continue
            plan.planner, plan.worker = mc.name, wmc.name
            used = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
            if used:
                say(session, f"📐 plan written by {mc.name} ({used:,} tokens)")
            return plan
    raise PlanError(last_error or "no model could produce a valid plan")


# ---------------------------------------------------------------- execution

def _short(text: str, limit: int) -> str:
    """One line, cut at a word boundary."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[:limit].rsplit(" ", 1)[0].rstrip(",;:.—-") + "…"


def _relative(cwd: str, path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(cwd).resolve()))
    except (ValueError, OSError):
        return str(path)


def _ledger(plan: Plan) -> str:
    done = [t for t in plan.tasks if t.status == "done"]
    if not done:
        return ""
    lines = ["## Already done (by earlier steps)"]
    for t in done:
        files = f" [{', '.join(t.changed[:5])}]" if t.changed else ""
        lines.append(f"- {t.id}. {t.title}: {' '.join(t.report.split())[:LEDGER_CHARS]}{files}")
    return "\n".join(lines) + "\n\n"


def task_prompt(plan: Plan, task: Task) -> str:
    files = f"\nFiles: {', '.join(task.files)}" if task.files else ""
    constraints = f"\nDo not: {task.constraints}" if task.constraints else ""
    check = f"\nThis step is done when `{task.check}` succeeds." if task.check else ""
    return TASK_PROMPT.format(summary=plan.summary, ledger=_ledger(plan), n=plan.tasks.index(task) + 1,
                              total=len(plan.tasks), title=task.title, goal=task.goal,
                              files=files, constraints=constraints, check=check)


def _check_allowed(session, command: str) -> bool:
    """A planner-written check runs like a bash call: deny rules apply; the approved plan covers the rest
    unless plan approval is off, in which case it needs the usual yes."""
    from . import permissions
    rule = permissions.decide(session.rules, "bash", {"command": command}, session.cwd)
    if rule == "deny":
        return False
    from .permissions import commit_action
    if rule != "allow" and session.auto_approve and session.guard_commits and commit_action("bash", {"command": command}):
        return session.on_approve("bash", {"command": command}) in ("yes", "always")   # commits are always asked
    if rule == "allow" or session.auto_approve or session.settings.delegate_approval:
        return True
    return session.on_approve("bash", {"command": command}) in ("yes", "always")


def run_check(session, task: Task) -> tuple[bool, str]:
    from .tools import ToolCancelled, run_cancellable
    if not task.check:
        return True, ""
    if not _check_allowed(session, task.check):
        return False, "the check command was blocked by a permission rule"
    try:
        proc = run_cancellable(["/bin/bash", "-lc", task.check], cwd=session.cwd, timeout=CHECK_TIMEOUT,
                               stop=session._stop)
    except ToolCancelled:
        return False, "cancelled"
    except subprocess.TimeoutExpired:
        return False, f"the check timed out after {CHECK_TIMEOUT}s"
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    return proc.returncode == 0, out[-1500:]


def _run_task(session, plan: Plan, task: Task, helper_tools: list, system: str) -> bool:
    """Run one task in a fresh helper. True when its check passes."""
    wmc, pmc = worker_model(session), planner_model(session)
    where = f"step {plan.tasks.index(task) + 1}/{len(plan.tasks)}"
    session.set_active_model(wmc, where)
    helper = make_helper(session, wmc, system, helper_tools)
    errors: list = []
    quiet = {"turn_end", "turn_start"}

    def forward(event, *p, **k):
        if event == "turn_end":
            if p and isinstance(p[0], dict) and p[0].get("error"):
                errors.append(p[0]["error"])
            return
        if event not in quiet:
            session.on_event(event, *p, **k)

    helper.on_event = forward
    helper.on_approve = session.on_approve
    link = _link_stop(session, helper)
    retries = max(0, session.settings.delegate_retries)
    escalated = False
    prompt = task_prompt(plan, task)
    try:
        attempt = 0
        while True:
            errors.clear()
            helper.run_turn(prompt)
            if session._stop.is_set():
                return False
            report = _last_text(helper)
            task.report = report or task.report
            task.changed = sorted({_relative(session.cwd, p) for ck in helper.checkpoints for p, _ in ck})
            originals = session.__dict__.setdefault("_plan_originals", {})
            for ck in helper.checkpoints:
                for path, before in ck:
                    originals.setdefault(str(path), before)          # the oldest state wins: what the review diffs against
            if errors:
                failure = errors[-1]
            else:
                ok, output = run_check(session, task)
                if ok:
                    return True
                failure = f"the check `{task.check}` failed:\n{output}"
            if session._stop.is_set():
                return False
            attempt += 1
            if attempt <= retries:
                say(session, f"↻ task {task.id}: {str(failure).splitlines()[0][:100]} — retrying ({attempt}/{retries})")
            elif not escalated and pmc.name != wmc.name:
                escalated = True
                say(session, f"⬆ task {task.id} is stuck on {wmc.name} — handing it to {pmc.name}")
                helper.model_config = pmc
                session.set_active_model(pmc, f"{where}, escalated")
            else:
                task.report = f"failed: {str(failure)[:300]}"
                return False
            prompt = (f"That did not work: {failure}\nFix it and finish this step. "
                      "Reply with a short report of what you changed.")
    finally:
        link.set()
        session.merge_usage(helper)


def execute(session, plan: Plan, resumed: bool = False) -> bool:
    """Run the plan's pending tasks in order. True when every task is done."""
    tools = [t for t in session.tools if t.name != "task"]
    total = len(plan.tasks)
    for task in plan.tasks:
        if task.status == "done":
            continue
        if session._stop.is_set():
            break
        if session.has_pending():        # you said something: stop here so it gets read, resume afterwards
            say(session, "⏸ pausing the plan because you sent a message — /delegate resume continues it")
            break
        task.status = "running"
        n = plan.tasks.index(task) + 1
        say(session, f"▶ step {n}/{total}: {task.title}")
        save_plan(session, plan)
        sync_todo(session, plan, left_off=f"running step {n}/{total}: {task.title}")
        ok = _run_task(session, plan, task, tools, session.system_prompt)
        task.status = "done" if ok else ("pending" if session._stop.is_set() else "failed")
        save_plan(session, plan)
        if ok:
            say(session, f"✓ step {n}/{total} done")
        else:
            reason = "cancelled" if session._stop.is_set() else task.report
            sync_todo(session, plan, left_off=f"stopped at step {n}/{total}: {reason[:120]}")
            return False
    sync_todo(session, plan, left_off="plan finished")
    return all(t.status == "done" for t in plan.tasks)


def summary_text(plan: Plan, finished: bool) -> str:
    done = sum(1 for t in plan.tasks if t.status == "done")
    head = (f"Plan complete: {done}/{len(plan.tasks)} tasks done." if finished
            else f"Plan stopped: {done}/{len(plan.tasks)} tasks done. `/delegate resume` continues from the first unfinished one.")
    lines = [head, f"({plan.summary})", ""]
    for t in plan.tasks:
        mark = {"done": "✓", "failed": "✗", "running": "…"}.get(t.status, "·")
        detail = _short(t.report, 110) if t.status in ("done", "failed") else ""
        lines.append(f"- {mark} {t.id}. {t.title}" + (f" — {detail}" if detail else ""))
    if plan.review:
        lines += ["", f"Review: {plan.review}"]
    changed = sorted({f for t in plan.tasks for f in t.changed})
    if changed:
        lines += ["", "Files changed: " + ", ".join(f"`{f}`" for f in changed[:20])]
    return "\n".join(lines)


# ---------------------------------------------------------------- review

def review_enabled(session) -> bool:
    mode = getattr(session.settings, "delegate_review", "auto")
    if mode == "off":
        return False
    return mode == "always" or planner_model(session).name != worker_model(session).name


def _diff_text(rel: str, before: bytes | None, after: bytes | None) -> str:
    import difflib
    try:
        b = (before or b"").decode("utf-8")
        a = (after or b"").decode("utf-8")
    except UnicodeDecodeError:
        return f"(binary file {rel} changed)\n"
    lines = list(difflib.unified_diff(b.splitlines(True), a.splitlines(True),
                                      fromfile=f"a/{rel}" if before is not None else "/dev/null",
                                      tofile=f"b/{rel}" if after is not None else "/dev/null"))
    text = "".join(lines)
    if not text.endswith("\n"):
        text += "\n\\ No newline at end of file\n"
    if len(text) > REVIEW_FILE_CHARS:
        text = text[:REVIEW_FILE_CHARS * 2 // 3] + "\n[... diff of this file truncated ...]\n" + text[-REVIEW_FILE_CHARS // 3:]
    return text


def collect_changes(session, plan: Plan) -> str:
    """The combined diff of every file the plan changed. Uses the originals remembered during this run; for a plan
    resumed after a restart it falls back to `git diff` (empty outside a repository)."""
    originals = session.__dict__.get("_plan_originals", {})
    files = sorted({f for t in plan.tasks for f in t.changed})
    chunks, missing = [], []
    for rel in files:
        path = (Path(session.cwd) / rel).resolve()
        key = next((k for k in (str(path), str(Path(session.cwd) / rel)) if k in originals), None)
        if key is None:
            missing.append(rel)
            continue
        try:
            after = path.read_bytes() if path.is_file() else None
        except OSError:
            after = None
        chunks.append(_diff_text(rel, originals[key], after))
    if missing:
        try:
            out = subprocess.run(["git", "-C", session.cwd, "diff", "HEAD", "--", *missing], capture_output=True,
                                 text=True, timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        if out.strip():
            chunks.append(out)
    text = "".join(chunks)
    if len(text) > REVIEW_DIFF_CHARS:
        text = text[:REVIEW_DIFF_CHARS] + "\n[... the rest of the diff is not shown ...]\n"
    return text


def parse_review(text: str) -> dict:
    raw = _json_from(text)
    verdict = str(raw.get("verdict", "")).lower()
    if verdict not in ("ok", "fix"):
        raise ValueError("verdict must be ok or fix")
    issues = []
    for item in (raw.get("issues") or [])[:MAX_REVIEW_ISSUES]:
        if isinstance(item, dict) and str(item.get("problem", "")).strip():
            issues.append({"file": str(item.get("file", "")).strip(), "problem": str(item["problem"]).strip()[:600],
                           "fix": str(item.get("fix", "")).strip()[:900]})
    return {"verdict": "fix" if (verdict == "fix" and issues) else "ok",
            "summary": str(raw.get("summary", "")).strip()[:300], "issues": issues}


def review_plan(session, plan: Plan) -> dict | None:
    """Ask the planner model to review the finished change. None when there is nothing to review or it can't."""
    diff = collect_changes(session, plan)
    if not diff.strip():
        return None
    pmc = planner_model(session)
    session.set_active_model(pmc, "reviewing")
    steps = "\n".join(f"{t.id}. {t.title} — {'check passed' if t.check else 'no check'}"
                      f"{(': ' + _short(t.report, 160)) if t.report else ''}" for t in plan.tasks)
    user = (f"## Request\n{plan.request[:4000]}\n\n## Plan: {plan.summary}\n{steps}\n\n## Combined diff\n{diff}")
    try:
        text, usage = complete(session, pmc, REVIEW_SYSTEM, user, max_tokens=1800)
        result = parse_review(text)
    except Exception as exc:  # noqa: BLE001 - a failed review must never undo a finished plan
        say(session, f"⚠ the review by {pmc.name} could not be completed ({str(exc)[:100] or type(exc).__name__})")
        return None
    used = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
    result["tokens"], result["by"] = used, pmc.name
    return result


def fix_tasks_from(plan: Plan, issues: list) -> list:
    """Turn review issues into steps; each reuses the check of the earlier step that owned the file, if any."""
    out, next_id = [], max(t.id for t in plan.tasks) + 1
    for issue in issues:
        owner = next((t for t in reversed(plan.tasks) if issue["file"] and issue["file"] in t.files and t.check), None)
        goal = issue["problem"] + (f"\nThe exact change to make: {issue['fix']}" if issue["fix"] else "")
        out.append(Task(id=next_id, title=f"Fix: {_short(issue['problem'], 70)}", goal=goal,
                        files=[issue["file"]] if issue["file"] else [], check=owner.check if owner else "",
                        depends_on=[]))
        next_id += 1
    return out


def review_and_fix(session, plan: Plan) -> bool:
    """After every step passed: one review round. True when the plan is (still) fully done."""
    if not review_enabled(session) or session._stop.is_set():
        return True
    say(session, f"🔎 reviewing the change with {planner_model(session).name} …")
    result = review_plan(session, plan)
    if result is None:
        return True
    tokens = f" ({result['tokens']:,} tokens)" if result.get("tokens") else ""
    if result["verdict"] == "ok":
        plan.review = f"no problems found by {result['by']}"
        say(session, f"✓ review by {result['by']}{tokens}: no problems found")
        save_plan(session, plan)
        return True
    issues = result["issues"]
    say(session, f"🔎 review by {result['by']}{tokens}: {len(issues)} problem(s) — fixing:\n"
        + "\n".join(f"  • {_short(i['problem'], 110)}" for i in issues))
    new = fix_tasks_from(plan, issues)
    plan.tasks.extend(new)
    plan.review = f"{len(issues)} problem(s) found by {result['by']}"
    save_plan(session, plan)
    ok = execute(session, plan)
    plan.review += " and fixed" if ok else " — not all fixed"
    save_plan(session, plan)
    return ok


# ---------------------------------------------------------------- entry points used by the session

def _approve_plan(session, plan: Plan) -> bool:
    if not session.settings.delegate_approval:
        return True
    verdict = session.on_approve("delegate-plan", {"text": format_plan(plan, plan.planner, plan.worker)})
    if verdict == "always":
        session.settings.delegate_approval = False      # for this session only
    return verdict in ("yes", "always")


def _finish(session, plan: Plan, finished: bool) -> None:
    text = summary_text(plan, finished)
    session.messages.append({"role": "assistant", "content": text})
    session.on_event("text", text)
    session.on_event("turn_end")
    session.save()


def _run_whole(session, request: str) -> bool:
    say(session, f"📐 gathering context with {worker_model(session).name} …")
    brief = build_brief(session, request)
    if session._stop.is_set():
        session.on_event("turn_end", aborted=True)
        return True
    say(session, f"📐 planning with {planner_model(session).name} …")
    try:
        plan = make_plan(session, request, brief)
    except PlanError as exc:
        if session._stop.is_set():
            session.on_event("turn_end", aborted=True)
            return True
        say(session, f"⚠ could not make a plan ({exc}) — handling it as a normal request")
        return False
    if not _approve_plan(session, plan):
        say(session, "plan declined — handling it as a normal request")
        return False
    save_plan(session, plan)
    sync_todo(session, plan, left_off="starting")
    finished = execute(session, plan)
    if finished and not session._stop.is_set():
        finished = review_and_fix(session, plan)
    if session._stop.is_set():
        session.on_event("turn_end", aborted=True)
        return True
    _finish(session, plan, finished)
    return True


def resume(session) -> bool:
    plan = latest_unfinished(session.cwd)
    if plan is None:
        say(session, "no unfinished plan to resume")
        session.on_event("turn_end")
        return True
    for t in plan.tasks:
        if t.status in ("running", "failed"):
            t.status = "pending"
    say(session, f"▶ resuming: {plan.summary}")
    finished = execute(session, plan, resumed=True)
    if finished and not session._stop.is_set():
        finished = review_and_fix(session, plan)
    if session._stop.is_set():
        session.on_event("turn_end", aborted=True)
        return True
    _finish(session, plan, finished)
    return True


def maybe_delegate(session, request: str, *, forced: bool = False) -> bool:
    """Called once per turn, after the user's message is in the history. True = this module handled the
    whole turn (and ended it); False = carry on as a normal turn."""
    st = session.settings
    if request.strip().lower() == "resume" and forced:
        try:
            return resume(session)
        finally:
            session.active_model = ""
            session.on_event("model_active")
    if not forced:
        if st.auto_delegate == "off" or session.plan_mode:
            return False
        large, why = triage(session, request)
        if not large:
            return False
        say(session, f"📐 this looks like a large task ({why})")
        if st.auto_delegate == "ask":
            verdict = session.on_approve("delegate", {"text": (
                f"This looks like a large task ({why}).\n\n"
                f"Let {planner_model(session).name} write a plan of small steps and {worker_model(session).name} "
                "carry them out?\n\nNo = handle it as a normal request.")})
            if verdict == "always":
                st.auto_delegate = "auto"                # for this session only
            elif verdict != "yes":
                return False
    try:
        return _run_whole(session, request)
    finally:
        session.active_model = ""                                # back to showing the session's own model
        session.on_event("model_active")
