"""Session state: the agentic loop, token accounting, and auto-compaction."""

import json
import queue
import threading
import time
import uuid

import httpx
from collections.abc import Callable
from pathlib import Path

from . import consult
from . import llm
from . import todo as todo_mod
from .config import ModelConfig, Settings, home_data_dir
from .tools import ToolResult, CACHEABLE_TOOLS

COMPACT_PROMPT = """\
Your conversation is being auto-compacted to stay within the context window.
Summarize EVERYTHING above — all exchanges, all tool results — into a detailed,
information-dense markdown summary that lets you continue as if uninterrupted.
Use exactly these sections:

## Primary Task
What the user asked for, in their words, plus any refinements they made along the way.

## Recent Work
Every file read/written/edited (with paths), commands run, and tool results of note.

## Key Decisions & User Feedback
Choices made and why; explicit user preferences and corrections to preserve exactly.

## Errors & Fixes
Problems encountered and how each was resolved.

## Current State
The precise state when the conversation was cut: what is done, what is in flight,
current branch/diff/build status.

## Next Steps
What remains, so work can resume straight away.

## Durable Facts
Bullets of stable facts worth remembering in FUTURE sessions — build/test/run
commands, project conventions and layout, environment details, the user's stated
preferences. Not task progress. Omit the section if there are none.

Be dense, not exhaustive: exact paths, flags, names and numbers, but no narration, and leave out
anything that can simply be read again from the files. This is the replacement for everything that
gets dropped, but it must stay under about 900 words — a long summary is slow to write and slow to read.

ACCURACY RULES — the summary replaces your memory, so a wrong line in it becomes a "fact":
- Record only what the transcript actually shows. Never infer an outcome. A command, test run or
  process that was started but whose result you did not see is "started, outcome not seen" /
  "still running" — never "completed" or "passed".
- Copy numbers exactly as they were printed. Do not compute totals, percentages or counts that
  nobody printed, and do not turn a guess about a cause into a finding: label guesses
  "hypothesis (unverified)".
- If the transcript starts with an earlier <auto-compaction> summary, carry its still-valid facts
  forward unchanged and mark anything it said that later messages contradicted as superseded. Do not
  restate its conclusions as new discoveries.
- Under Key Decisions & User Feedback copy the user's corrections and standing instructions
  ("stop doing X", "we use Y, not Z") word for word. The user's later messages override anything else.
- Next Steps: only what the user asked for or what was explicitly in progress. Do not add new
  ideas, merges, deployments or clean-ups the user did not ask for.
"""

STEERING_NOTE = ("[The user sent this message while you were working. It takes priority over what you were "
                 "doing: read it now, acknowledge it, and change course if it says to.]\n\n")

COMPACT_MAX_TOKENS = 3000        # hard cap on a summary: on a local model every 1,000 tokens written costs minutes

ON_COMPACTION_NOTE = (
    "This session was auto-compacted: everything above this point is a summary of the "
    "earlier conversation. The user's instructions in it still apply, but the summary can be "
    "incomplete or wrong about the CURRENT state of processes, containers, files and test runs: "
    "before acting on a claim about system state, verify it with a quick read-only command. "
    "The user's messages after this point always override the summary. Continue the work in "
    "Next Steps unless the user says otherwise."
)


DEGENERATE_HELP = (
    "the model's output degenerated into a repeating pattern, so hexiel stopped it. This is "
    "usually a model-server problem (e.g. flash attention on some GPUs/iGPUs, or a context "
    "longer than the server's limit) — try /compact, restarting the model server, or /model")


def _degenerate(tail: str) -> bool:
    """A short unit repeated 40+ times at the end of the output (e.g. '#!#!#!…')."""
    import re as _re
    return bool(_re.search(r"(.{1,8}?)\1{39,}$", tail, _re.S)) and len(set(tail[-200:])) <= 8


SUBAGENT_SYSTEM = (
    "You are a focused research subagent working for a coding agent. Investigate the task with "
    "the read-only tools available (search, read, map, web), then reply with a concise, factual "
    "answer: findings, exact file paths and line numbers, and anything uncertain. Do not ask "
    "questions back; you cannot modify anything.")
SUBAGENT_LIMIT = "\n\n(Keep your final answer under ~400 words; include paths and line numbers.)"

FILE_TOOLS = {"write", "edit", "image_edit"}      # snapshotted before they run (/undo)
PLAN_MODE_ALLOWED = {"todo", "memory", "skill"}   # bookkeeping still works in plan mode
PLAN_MODE_NOTE = ("[plan mode] Investigate with read-only tools and produce a step-by-step plan. "
                  "Do not try to change files or run commands; the user will approve the plan "
                  "and turn plan mode off.")

EMPTY_NUDGE = ("(Your previous reply was empty. Continue the task: either call the next tool "
               "or reply to the user with your answer.)")


def _is_empty_reply(msg: dict) -> bool:
    return (msg.get("role") == "assistant" and not (msg.get("content") or "").strip()
            and not msg.get("tool_calls"))


MAX_TIMEOUT_RETRIES = 2   # consecutive request timeouts before giving up (each already waited request_timeout)


class Session:
    def __init__(self, model_config: ModelConfig, settings: Settings, cwd: str,
                 tools: list, system_prompt: str, on_event: Callable = None,
                 on_approve: Callable = None):
        self.model_config = model_config
        self.settings = settings
        self.cwd = cwd
        self.tools = tools
        self.system_prompt = system_prompt
        self._on_event = on_event or (lambda *_a, **_k: None)
        self._on_approve = on_approve or (lambda *_: "yes")
        self.web_base = ""                       # the local web UI's address, once one is serving this session
        self.mirrors: list = []                  # other views of this chat (Hexiel Remote): see add_mirror
        self.dismiss_approval = lambda: None     # the primary UI closes its approval prompt when a mirror answers first
        self.id = uuid.uuid4().hex[:12]
        self.created = time.time()
        self.messages: list[dict] = []
        self.mcp_servers: list = []   # started MCP servers (see mcp_client)
        self.plan_mode = False          # read-only planning: no writes/commands run
        self.loaded_toolsets: set = set()   # optional tool groups in use (toolsets.py)
        self.checkpoints: list[list] = []   # per turn: [(path, original bytes | None)] for /undo
        from . import hooks as hooks_mod
        self.hooks = hooks_mod.load(cwd)     # user commands on agent events (hooks.py)
        self.usage_totals = {"requests": 0, "prompt_tokens": 0, "cached_tokens": 0,
                             "completion_tokens": 0}
        self._provider_override = None  # set when auto-resume switches to the fallback profile
        from .permissions import Rules
        self.rules = Rules()            # allow/ask/deny rules (permissions.py)
        self.last_usage: dict = {}
        self.total_completion_tokens = 0
        self.auto_approve = settings.auto_approve
        self.guard_commits = settings.guard_commits       # auto-approve, but still ask before git/svn commits
        self.compacting = False
        self.usage_by_model: dict[str, dict] = {}     # model id -> {requests, prompt, cached, completion}: what each model cost
        self.internal = False      # True for hexiel's own helper sessions (delegated steps, briefings): their prompts are not the user's
        self.active_model = ""     # "model · role" while a /delegate role (planner/worker) is working; "" otherwise
        self._usage_msgs = 0       # len(messages) when the request behind last_usage was sent
        self._pending: list[str] = []          # messages the user typed while a turn was running
        self._pending_lock = threading.Lock()
        self._stop = threading.Event()
        self._tool_ctx = None  # bind with bind_tool_ctx() before run_turn

    # ---------------------------------------------------------------- run turn

    def stop(self) -> None:
        self._stop.set()

    def record_usage(self, model: str, usage: dict | None) -> None:
        if not usage:
            return
        u = self.usage_by_model.setdefault(model, {"requests": 0, "prompt": 0, "cached": 0, "completion": 0})
        u["requests"] += 1
        u["prompt"] += usage.get("prompt_tokens", 0)
        u["cached"] += usage.get("cached_tokens", 0)
        u["completion"] += usage.get("completion_tokens", 0)

    def merge_usage(self, other: "Session") -> None:
        """Fold a helper session's per-model usage into this one (delegated steps, briefings, subagents)."""
        for model, u in other.usage_by_model.items():
            mine = self.usage_by_model.setdefault(model, {"requests": 0, "prompt": 0, "cached": 0, "completion": 0})
            for key, value in u.items():
                mine[key] += value

    def set_active_model(self, mc, role: str = "") -> None:
        """Tell the UIs which model is doing the work right now (the status line shows it)."""
        self.active_model = f"{mc.model} · {role}" if role else ""
        self.on_event("model_active")

    # -- messages typed while a turn is running ("steering")

    def queue_message(self, text: str) -> None:
        """The user typed something while hexiel was busy. It is read by the model before its next step
        (or answered as a follow-up turn if the turn ends first) — never dropped."""
        text = text.strip()
        if text:
            with self._pending_lock:
                self._pending.append(text)

    def has_pending(self) -> bool:
        with self._pending_lock:
            return bool(self._pending)

    def take_pending(self) -> list[str]:
        with self._pending_lock:
            items, self._pending = self._pending, []
        return items

    def _deliver_pending(self) -> None:
        items = self.take_pending()
        if not items:
            return
        for text in items:
            self.messages.append({"role": "user", "content": STEERING_NOTE + text})
            consult.note_user_message(self, text)
        self.on_event("note", "↳ hexiel read your new message" if len(items) == 1
                      else f"↳ hexiel read your {len(items)} new messages")
        self.save()

    # The primary UI (terminal or web) owns on_event / on_approve and swaps them freely. Mirrors — a browser
    # driving this same chat through Hexiel Remote — see every event too, and may answer an approval.
    @property
    def on_event(self):
        primary, mirrors = self._on_event, self.mirrors
        if not mirrors:
            return primary

        def fan_out(event, *payload, **kw):
            primary(event, *payload, **kw)
            for mirror in list(mirrors):
                try:
                    mirror.on_event(event, *payload, **kw)
                except Exception:  # noqa: BLE001 - a broken mirror must not break the chat
                    pass
        return fan_out

    @on_event.setter
    def on_event(self, fn) -> None:
        self._on_event = fn

    @property
    def on_approve(self):
        primary, mirrors = self._on_approve, self.mirrors
        if not any(m.has_viewers() for m in mirrors):
            return primary
        return lambda *args: self._ask_everywhere(primary, list(mirrors), args)

    @on_approve.setter
    def on_approve(self, fn) -> None:
        self._on_approve = fn

    def add_mirror(self, mirror) -> None:
        if mirror not in self.mirrors:
            self.mirrors.append(mirror)

    def remove_mirror(self, mirror) -> None:
        if mirror in self.mirrors:
            self.mirrors.remove(mirror)

    def _ask_everywhere(self, primary, mirrors: list, args: tuple) -> str:
        """Ask the primary UI and every mirror with a viewer at once; the first answer wins and the
        other prompts are closed."""
        answers: "queue.Queue[tuple[object, str]]" = queue.Queue()

        def ask(who, fn):
            try:
                answers.put((who, fn(*args)))
            except Exception:  # noqa: BLE001
                answers.put((who, "no"))
        askers = [(primary, primary)] + [(m, m.ask_approval) for m in mirrors if m.has_viewers()]
        for who, fn in askers:
            threading.Thread(target=ask, args=(who, fn), daemon=True).start()
        winner, verdict = answers.get()
        for who, _ in askers:
            if who is winner:
                continue
            if who is primary:
                self.dismiss_approval()
            else:
                who.cancel_approvals()
        return verdict

    def run_turn(self, user_text: str) -> None:
        self._stop.clear()
        try:
            self._turn_body(user_text)
        except Exception as exc:  # noqa: BLE001 - record incident, keep the UI alive
            import traceback
            from . import heal as heal_mod
            trail = traceback.format_exc()
            heal_mod.record_incident(self.cwd, kind="turn-crash",
                                     detail=f"{type(exc).__name__}: {exc}",
                                     traceback_text=trail,
                                     context={"user_text": user_text[:500]})
            self.on_event("incident", "turn", trail[-400:])
            raise

    def _turn_body(self, user_text: str) -> None:
        from .tools import attach_images
        delegate_mode = None                       # /delegate forces it, /solo opts out of the automatic check
        cloud_turn = False                         # /cloud: the strong model answers this request itself
        if user_text == "/cloud" or user_text.startswith("/cloud "):
            user_text, cloud_turn = user_text[len("/cloud"):].strip(), True
        for prefix, mode in (("/delegate", "force"), ("/solo", "skip")):
            if user_text == prefix or user_text.startswith(prefix + " "):
                user_text, delegate_mode = user_text[len(prefix):].strip(), mode
                break
        self._turn_request = user_text            # what consulting the strong model is told the user asked for
        consult.reset_turn(self)
        if not self.internal and any(m.get("role") == "assistant" for m in self.messages):
            consult.note_user_message(self, user_text)        # a correction right after hexiel's last answer counts
        if cloud_turn and user_text:
            if not (consult.strong_available(self) and consult.escalate(self, "you asked for the cloud model", steps=10 ** 6)):
                self.on_event("note", "no stronger model is available — set a Planner model in /config; carrying on locally")
        if delegate_mode == "force" and not user_text:
            self.on_event("note", "usage: /delegate <request>  — or /delegate resume to continue an unfinished plan")
            self.on_event("turn_end")
            return
        images, notes = attach_images(user_text, self.cwd)
        entry = {"role": "user", "content": "\n".join(
            [user_text, *notes, *([PLAN_MODE_NOTE] if self.plan_mode else [])])}
        if images:
            entry["images"] = images
        from . import instructions as instructions_mod
        topics = instructions_mod.relevant(self.cwd, user_text, self.model_config.context_window,
                                           self.__dict__.setdefault("_instr_sent", set()))
        if topics:
            entry["content"] += f"\n\n[project instructions relevant to this request]\n{topics}"
        self.checkpoints.append([])        # file snapshots taken during this turn
        from . import hooks as hooks_mod
        for out in hooks_mod.run(self.hooks, "UserPromptSubmit", self.cwd, self.id, prompt=user_text):
            if out.blocked:
                self.on_event("turn_end", {"error": f"blocked by a UserPromptSubmit hook: {out.stderr or out.command}"})
                return
            if out.code == 0 and out.stdout:
                entry["content"] += f"\n\n[hook context]\n{out.stdout[:4000]}"
        # empty assistant replies teach the model to keep answering with nothing
        self.messages = [m for m in self.messages if not _is_empty_reply(m)]
        self.messages.append(entry)
        self.save()
        if delegate_mode != "skip":
            from . import planner
            if planner.maybe_delegate(self, user_text, forced=delegate_mode == "force"):
                return
        provider = llm.make_provider(self.model_config, self.settings)
        tool_defs = [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in self.tools
        ]
        turn = {"tools": 0, "used_todo": False, "item": None, "ok": False}
        self._provider_override = None   # a fallback switch already changed model_config
        try:
            while True:
                if self._stop.is_set():
                    self.on_event("turn_end", aborted=True)
                    return
                self._deliver_pending()
                try:
                    consult.maybe_help(self)               # struggling? advice, or the strong model takes over for a while
                except Exception:  # noqa: BLE001 - help is a bonus, never a reason to stop
                    pass
                if self.compaction_needed() and not self.compacting:
                    self.compact(self._provider_override or provider)
                tool_defs = [{"name": t.name, "description": t.description, "parameters": t.parameters}
                             for t in self.tools]        # load_tools may have added a group
                self.on_event("request", self.tokens_now())
                assistant = self._stream_assistant(provider, tool_defs)
                if _is_empty_reply(assistant) and not getattr(self, "_provider_error", None) \
                        and not self._stop.is_set():
                    # some local models occasionally end their reply immediately (one
                    # end-of-turn token); nudge once instead of storing an empty reply
                    self.on_event("empty_retry")
                    self.messages.append({"role": "user", "content": EMPTY_NUDGE})
                    try:
                        assistant = self._stream_assistant(provider, tool_defs)
                    finally:
                        self.messages.pop()
                if _is_empty_reply(assistant):
                    error = getattr(self, "_provider_error", None) or (
                        "the model returned an empty reply twice — try rephrasing, /compact, "
                        "or /model to switch models")
                    self._provider_error = None
                    if not self._stop.is_set():
                        self.on_event("turn_end", {"error": error})
                    else:
                        self.on_event("turn_end", aborted=True)
                    return
                self.messages.append(assistant)
                self.save()
                calls = assistant.get("tool_calls") or []
                if not calls:
                    if self.has_pending():        # the user spoke while this reply was being written: answer them too
                        continue
                    turn["ok"] = True
                    self.on_event("turn_end")
                    return
                for call in calls:
                    if self._stop.is_set():
                        self._append_tool_results(calls[calls.index(call):])
                        self.on_event("turn_end", aborted=True)
                        return
                    self._execute_tool(call)
                    self._auto_todo_step(user_text, call, assistant, turn)
        finally:
            consult.end_burst(self, "request finished")      # always hand the next request back to the cheap model
            self._auto_todo_finish(turn)
            if turn["ok"]:
                self._auto_commit(user_text)
            from . import hooks as hooks_mod
            hooks_mod.run(self.hooks, "Stop", self.cwd, self.id)
            self.save()

    def _auto_commit(self, user_text: str) -> None:
        """Opt-in (settings.auto_commit): commit exactly the files hexiel changed
        this turn — never anything else in the working tree."""
        if not getattr(self.settings, "auto_commit", False) or not self.checkpoints:
            return
        import subprocess
        files = [str(p) for p, _ in self.checkpoints[-1]]
        if not files:
            return
        title = " ".join(user_text.split())[:60] or "changes"
        try:
            subprocess.run(["git", "-C", self.cwd, "add", "--", *files], capture_output=True, timeout=30)
            done = subprocess.run(["git", "-C", self.cwd, "commit", "-q", "-m", f"hexiel: {title}",
                                   "-m", "\n".join(f"- {Path(f).name}" for f in files), "--", *files],
                                  capture_output=True, text=True, timeout=30)
            if done.returncode == 0:
                self.on_event("auto_commit", len(files), title)
        except (OSError, subprocess.SubprocessError):
            pass

    # ---------------------------------------------------------------- automatic todo upkeep

    def _auto_todo_step(self, user_text: str, call: dict, assistant: dict, turn: dict) -> None:
        """Track multi-step requests and keep the checkpoint fresh — in Python,
        so it costs no tokens. Skipped when the model manages todo itself."""
        turn["tools"] += 1
        if call.get("name") == "todo":
            turn["used_todo"] = True
        if turn["used_todo"]:
            return
        try:
            if turn["item"] is None and turn["tools"] >= todo_mod.AUTO_MIN_TOOLS:
                turn["item"] = todo_mod.auto_track(self.cwd, user_text)
            if turn["item"] is None:
                return      # a quick question must not overwrite an ongoing task's checkpoint
            said = " ".join((assistant.get("content") or "").split())[:90]
            args = call.get("arguments") or {}
            first = next((str(v) for v in args.values() if v), "")[:60]
            todo_mod.auto_checkpoint(self.cwd, f"{call.get('name')}({first})" + (f" — {said}" if said else ""))
        except Exception:  # noqa: BLE001 - bookkeeping must never break a turn
            pass

    def _auto_todo_finish(self, turn: dict) -> None:
        if not turn.get("item"):
            return
        last = next((m.get("content", "") for m in reversed(self.messages)
                     if m.get("role") == "assistant" and m.get("content")), "")
        note = ("done: " if turn["ok"] else "interrupted: ") + " ".join(last.split())[:150]
        try:
            todo_mod.auto_finish(self.cwd, turn["item"], turn["ok"], note)
        except Exception:  # noqa: BLE001
            pass

    def _stream_assistant(self, provider, tool_defs) -> dict:
        """Stream one assistant response, waiting out rate limits (auto-resume)
        and switching to the fallback profile when a limit won't reset soon."""
        attempt = 0
        timeouts = 0
        while True:
            try:
                return self._stream_once(self._provider_override or provider, tool_defs)
            except llm.RateLimitError as exc:
                if isinstance(exc, llm.TransientError):
                    # not a rate limit: the server is down, unreachable or hung. A request that timed out
                    # already waited request_timeout seconds, so don't repeat that more than once.
                    timeouts += exc.timed_out
                    if timeouts >= MAX_TIMEOUT_RETRIES:
                        self._provider_error = (f"{exc} — the model server accepted the request but never answered "
                                                f"(it may be hung or overloaded). Check it, then send your message again.")
                        return {"role": "assistant", "content": ""}
                    wait = self._limit_wait(exc, attempt)
                    if wait is None:
                        self._provider_error = str(exc)
                        return {"role": "assistant", "content": ""}
                    self.on_event("model_unreachable", wait, str(exc)[:200])
                    if not self._sleep_interruptibly(wait):
                        return {"role": "assistant", "content": ""}
                    attempt += 1
                    continue
                wait = self._limit_wait(exc, attempt)
                if wait is None:
                    if self._switch_to_fallback(str(exc)):
                        attempt = 0
                        continue
                    self._provider_error = str(exc) + (
                        "" if self.settings.auto_resume else " (auto-resume is off)")
                    return {"role": "assistant", "content": ""}
                self.on_event("rate_limited", wait, str(exc)[:200])
                if not self._sleep_interruptibly(wait):
                    return {"role": "assistant", "content": ""}
                self.on_event("resumed")
                attempt += 1

    def _interruptible(self, stream, provider):
        """Yield from a provider stream while staying responsive to stop(): the request runs in a worker
        thread, so Ctrl+C works even while the model server is silent (reading a long prompt can take
        minutes). On stop the connection is cut, which also makes the server abandon the request."""
        items: queue.Queue = queue.Queue()
        done = object()
        cancelled = threading.Event()

        def pump():
            try:
                for item in stream:
                    if cancelled.is_set():
                        break
                    items.put(item)
            except BaseException as exc:  # noqa: BLE001 - re-raised in the consuming thread
                if not cancelled.is_set():
                    items.put(exc)
            finally:
                try:
                    getattr(stream, "close", lambda: None)()
                finally:
                    items.put(done)

        threading.Thread(target=pump, daemon=True, name="hexiel-stream").start()
        try:
            while True:
                try:
                    item = items.get(timeout=0.2)
                except queue.Empty:
                    if self._stop.is_set():
                        return
                    continue
                if item is done:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            if self._stop.is_set():
                cancelled.set()
                abort = getattr(provider, "abort", None)
                if abort:
                    abort()

    def _sleep_interruptibly(self, seconds: float) -> bool:
        """Sleep, waking early if the turn is stopped. False when it was stopped."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._stop.is_set():
                return False
            time.sleep(min(1.0, deadline - time.time()))
        return True

    def _limit_wait(self, exc: "llm.RateLimitError", attempt: int) -> float | None:
        """Seconds to wait before retrying, or None to stop waiting."""
        if not self.settings.auto_resume or exc.long_term and exc.retry_after is None:
            return None
        wait = exc.retry_after if exc.retry_after is not None else min(30 * 2 ** attempt, 900)
        wait += 2                                         # land just after the reset
        if wait > self.settings.auto_resume_max_wait or attempt >= 12:
            return None
        return wait

    def _switch_to_fallback(self, reason: str) -> bool:
        name = self.settings.fallback_profile
        if not name or name == self.model_config.name:
            return False
        from .config import load_config
        try:
            fallback = load_config().model(name)
        except (KeyError, SystemExit):
            return False
        self.on_event("fallback", self.model_config.name, name, reason[:200])
        self.model_config = fallback
        self._provider_override = llm.make_provider(fallback, self.settings)
        return True

    def _stream_once(self, provider, tool_defs) -> dict:
        """Stream one assistant response; returns the finalized message dict."""
        final = None
        reasoning: list[str] = []
        streamed: list[str] = []
        sent = len(self.messages)       # the prompt covers exactly these messages
        try:
            stream = provider.stream(self.messages, self.system_prompt, tool_defs,
                                     model=self.model_config.model,
                                     max_output_tokens=self.model_config.max_output_tokens,
                                     extra_body=self.model_config.extra_body,
                                     timeout=self.settings.request_timeout,
                                     echo_reasoning=self.model_config.echo_reasoning,
                                     max_tokens_param=self.model_config.max_tokens_param)
            for event, *payload in self._interruptible(stream, provider):
                if self._stop.is_set():
                    break
                if event == "text":
                    self.on_event("text", payload[0])
                    streamed.append(payload[0])
                    if len(streamed) % 20 == 0 and _degenerate("".join(streamed)[-600:]):
                        self._provider_error = DEGENERATE_HELP
                        return {"role": "assistant", "content": ""}
                elif event == "thinking":
                    self.on_event("thinking", payload[0])
                    if self.model_config.echo_reasoning:
                        reasoning.append(payload[0])
                elif event == "assistant":
                    final = payload[0]
                elif event == "done":
                    usage = payload[0]
                    if usage:
                        self.last_usage = usage
                        self._usage_msgs = sent
                        self.record_usage(self.model_config.model, usage)
                        t = self.usage_totals
                        t["requests"] += 1
                        t["prompt_tokens"] += usage.get("prompt_tokens", 0)
                        t["cached_tokens"] += usage.get("cached_tokens", 0)
                        t["completion_tokens"] += usage.get("completion_tokens", 0)
                        self.total_completion_tokens += usage.get("completion_tokens", 0)
                        self.on_event("usage", usage)
        except llm.RateLimitError:
            if final is None and not reasoning:
                raise                      # nothing streamed yet: safe to wait and retry
            self._provider_error = "rate limited mid-response"
        except httpx.TransportError as exc:
            # a timeout or dropped connection is not a bug in hexiel: retry when nothing
            # has streamed yet, otherwise end the turn with a clear message
            if final is None and not reasoning and not streamed:
                timed_out = isinstance(exc, httpx.TimeoutException)
                raise llm.TransientError(
                    f"the model server didn't answer within {self.settings.request_timeout}s" if timed_out
                    else f"the connection to the model server failed ({type(exc).__name__}) — retrying",
                    timed_out=timed_out) from exc
            self._provider_error = (f"the connection to the model server dropped mid-reply "
                                    f"({type(exc).__name__}) — send your message again to retry")
        except llm.ProviderError as exc:
            self._provider_error = str(exc)
        final = final if final is not None else {"role": "assistant", "content": ""}
        if reasoning:
            final["reasoning"] = "".join(reasoning)
        return final

    # ---------------------------------------------------------------- tool execution

    def _execute_tool(self, call: dict) -> None:
        tool = next((t for t in self.tools if t.name == call["name"]), None)
        if tool is None:
            self.messages.append({"role": "tool", "tool_call_id": call["id"],
                                  "content": f"error: unknown tool '{call['name']}'",
                                  "is_error": True})
            return
        from . import permissions
        rule = permissions.decide(self.rules, tool.name, call["arguments"], self.cwd)
        if rule == "deny":
            self.messages.append({
                "role": "tool", "tool_call_id": call["id"], "is_error": True,
                "content": f"Blocked by a permission rule: {call['name']} is denied for this "
                           "target. Do not retry it; find another approach or ask the user."})
            self.on_event("tool_result", call, "blocked by a deny rule", True)
            return
        if self.plan_mode and not tool.readonly and tool.name not in PLAN_MODE_ALLOWED:
            self.messages.append({
                "role": "tool", "tool_call_id": call["id"], "is_error": True,
                "content": "Plan mode is on: no changes are made. Describe the change you "
                           "would make instead; the user will turn plan mode off to apply it."})
            self.on_event("tool_result", call, "skipped — plan mode", True)
            return
        skips_approval = getattr(tool, "always_ok", False) or rule == "allow"
        needs_ask = rule == "ask" or (not tool.readonly and not skips_approval and not self.auto_approve)
        guarded = False
        if (not needs_ask and self.auto_approve and self.guard_commits and rule != "allow"
                and not tool.readonly and not getattr(tool, "always_ok", False)):
            what = permissions.commit_action(tool.name, call["arguments"])
            if what:
                guarded = needs_ask = True
                self.on_event("note", f"🔒 this would run {what} — asking first "
                                      "(auto-approve is set to everything except commits)")
        if needs_ask:
            verdict = self.on_approve(tool.name, call["arguments"])
            if verdict == "always" and not guarded:
                # scoped: "always" for git status means git status, not every command
                self.rules.allow.append(permissions.always_rule(tool.name, call["arguments"]))
            elif verdict == "no":
                self.messages.append({
                    "role": "tool", "tool_call_id": call["id"], "is_error": True,
                    "content": f"User denied the {call['name']} tool call. "
                               "Find another approach or ask the user."})
                self.on_event("tool_result", call, "denied by user", True)
                return
        self.on_event("tool_call", call)
        cache_key = None
        if tool.name in CACHEABLE_TOOLS:
            cache_key = (tool.name, json.dumps(call["arguments"], sort_keys=True))
            hit = self.tool_ctx.result_cache.get(cache_key)
            if hit is not None:
                entry = {"role": "tool", "tool_call_id": call["id"],
                         "content": hit.text + "\n(cached — identical earlier call this session)"}
                self.messages.append(entry)
                self.on_event("tool_result", call, entry["content"], False, 0.0)
                return
        from . import hooks as hooks_mod
        for out in hooks_mod.run(self.hooks, "PreToolUse", self.cwd, self.id,
                                 tool=tool.name, tool_input=call["arguments"]):
            if out.blocked:
                reason = out.stderr or f"blocked by hook: {out.command}"
                self.messages.append({"role": "tool", "tool_call_id": call["id"], "is_error": True,
                                      "content": f"A PreToolUse hook blocked this call: {reason}"})
                self.on_event("tool_result", call, f"blocked by hook: {reason[:200]}", True)
                return
        self._snapshot(tool.name, call["arguments"])
        started = time.time()
        try:
            result = tool.run(call["arguments"], self.tool_ctx)
        except Exception as exc:  # noqa: BLE001 - record + recover, model continues
            import traceback
            from . import heal as heal_mod
            path = heal_mod.record_incident(self.cwd, kind=f"tool-{tool.name}",
                                            detail=f"{tool.name} raised {exc!r}",
                                            traceback_text=traceback.format_exc(),
                                            context={"arguments": call["arguments"]})
            self.on_event("incident", tool.name, str(path))
            result = ToolResult(text=f"internal error in {tool.name}: {exc!r} "
                                     f"(recorded to {path}); work around it or retry",
                                is_error=True)
        for out in hooks_mod.run(self.hooks, "PostToolUse", self.cwd, self.id, tool=tool.name,
                                 tool_input=call["arguments"], tool_response=result.text):
            if out.blocked and out.stderr:          # feedback for the model (e.g. lint errors)
                result = ToolResult(text=f"{result.text}\n\n[PostToolUse hook]\n{out.stderr[:4000]}",
                                    images=result.images, is_error=result.is_error)
        entry = {"role": "tool", "tool_call_id": call["id"], "content": result.text}
        if result.is_error:
            entry["is_error"] = True
        if getattr(result, "images", None):
            entry["images"] = result.images
        self.messages.append(entry)
        consult.record_result(self, call, bool(result.is_error), result.text)
        if cache_key is not None and not result.is_error:
            self.tool_ctx.result_cache[cache_key] = result
        self.on_event("tool_result", call, result.text, result.is_error,
                      time.time() - started)

    # ---------------------------------------------------------------- subagents

    def run_subagent(self, args: dict) -> "ToolResult":
        """The `task` tool: a helper with its own context and read-only tools does
        the digging; only its final answer comes back into this conversation."""
        from .config import load_config
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return ToolResult(text="error: prompt is required", is_error=True)
        model = self.model_config
        name = getattr(self.settings, "subagent_profile", "")
        if name:
            try:
                model = load_config().model(name)
            except (KeyError, SystemExit):
                pass
        tools = [t for t in self.tools if t.readonly and t.name != "task"]
        helper = Session(model, self.settings, self.cwd, tools, SUBAGENT_SYSTEM)
        helper.bind_tool_ctx(self.tool_ctx)
        helper.hooks = self.hooks
        helper.rules = self.rules
        label = str(args.get("description", "")).strip()[:60] or "subagent"
        helper.on_event = lambda event, *p, **k: (
            self.on_event("subagent", label, p[0].get("name", "")) if event == "tool_call" else None)
        helper.on_approve = lambda *a: "no"
        helper.save = lambda: None                  # helpers don't create saved sessions
        helper.run_turn(prompt + SUBAGENT_LIMIT)
        answer = next((m.get("content", "") for m in reversed(helper.messages)
                       if m.get("role") == "assistant" and m.get("content")), "")
        for key in self.usage_totals:
            self.usage_totals[key] += helper.usage_totals.get(key, 0)
        self.merge_usage(helper)
        steps = sum(1 for m in helper.messages if m.get("role") == "tool")
        if not answer:
            return ToolResult(text=f"the subagent finished without an answer after {steps} step(s)",
                              is_error=True)
        return ToolResult(text=f"[subagent: {steps} tool call(s), result only]\n{answer[:8000]}")

    # ---------------------------------------------------------------- checkpoints / undo

    def _snapshot(self, tool_name: str, args: dict) -> None:
        """Remember a file's content before a file tool changes it (for /undo)."""
        if tool_name not in FILE_TOOLS or not self.checkpoints or self.tool_ctx is None:
            return
        from .tools import _resolve_path
        raw = args.get("out") if tool_name == "image_edit" and args.get("out") else args.get("path")
        if not raw:
            return
        try:
            path = _resolve_path(raw, self.tool_ctx)
            if any(p == path for p, _ in self.checkpoints[-1]):
                return                       # keep the oldest state from this turn
            original = path.read_bytes() if path.is_file() else None
        except OSError:
            return
        self.checkpoints[-1].append((path, original))

    def undo(self) -> list[str]:
        """Revert the most recent turn that changed files; returns what was restored."""
        while self.checkpoints and not self.checkpoints[-1]:
            self.checkpoints.pop()
        if not self.checkpoints:
            return []
        restored = []
        for path, original in reversed(self.checkpoints.pop()):
            try:
                if original is None:
                    path.unlink(missing_ok=True)
                    restored.append(f"removed {path}")
                else:
                    path.write_bytes(original)
                    restored.append(f"restored {path}")
            except OSError as exc:
                restored.append(f"could not restore {path}: {exc}")
        return restored

    def _append_tool_results(self, remaining: list[dict]) -> None:
        for call in remaining:
            self.messages.append({"role": "tool", "tool_call_id": call["id"],
                                  "content": "interrupted by user", "is_error": True})

    @property
    def tool_ctx(self):
        if self._tool_ctx is None:
            raise RuntimeError("tool context not bound; call bind_tool_ctx() first")
        return self._tool_ctx

    def bind_tool_ctx(self, tool_ctx) -> None:
        self._tool_ctx = tool_ctx
        if tool_ctx.stop_event is None:       # lets Ctrl+C kill a command that is running (first binder wins)
            tool_ctx.stop_event = self._stop

    # ---------------------------------------------------------------- token accounting

    def tokens_now(self) -> int:
        """Size of the next request's prompt: the server's exact count for the last request, plus an
        estimate of everything appended since (the reply and tool results are not in that count)."""
        usage = self.last_usage.get("prompt_tokens") if self.last_usage else 0
        if usage:
            return usage + self._msgs_tokens(self.messages[self._usage_msgs:])
        return self.estimate_tokens()

    def estimate_tokens(self) -> int:
        return llm.est_tokens(self.system_prompt) + self._tool_schema_tokens() + self._msgs_tokens(self.messages)

    def _tool_schema_tokens(self) -> int:
        key = tuple(t.name for t in self.tools)
        if getattr(self, "_schema_cache", (None, 0))[0] != key:
            blob = json.dumps([{"name": t.name, "description": t.description, "parameters": t.parameters}
                               for t in self.tools])
            self._schema_cache = (key, max(llm.est_tokens(blob), 300))
        return self._schema_cache[1]

    @staticmethod
    def _msgs_tokens(messages: list[dict]) -> int:
        total = 0
        for msg in messages:
            total += llm.est_tokens(msg.get("content", "")) + 8
            for call in msg.get("tool_calls", []):
                total += llm.est_tokens(json.dumps(call.get("arguments", {}))) + 20
            for image in msg.get("images", []):
                total += image.get("approx_tokens", 800)
        return total

    def compaction_needed(self) -> bool:
        if not self.messages:
            return False
        current = max(self.estimate_tokens(), self.tokens_now())
        return current >= self.model_config.context_window * self.settings.compact_threshold

    # ---------------------------------------------------------------- compaction

    def _elide_stale_tools(self) -> int:
        """Token-economy pre-step: rewrite old tool results (everything before
        the kept tail) as one-line stamps so compaction summarizes cheap text.
        Returns the number of messages elided."""
        split = self._turn_split(self.messages)
        if split <= 0:
            return 0
        elided = 0
        for msg in self.messages[:split]:
            if msg.get("role") == "tool" and msg.get("content"):
                kind_guess = len(msg["content"])
                msg["content"] = f"[prior tool output elided: ~{kind_guess} chars]"
                elided += 1
            if msg.pop("reasoning", None):          # stale reasoning is never needed again
                elided += 1
            # old images would otherwise be re-sent on every request
            if msg.pop("images", None):
                msg["content"] = (msg.get("content") or "") + "\n[image elided from context]"
                elided += 1
        return elided

    def compact(self, provider, manual: bool = False) -> None:
        """Summarize the older history and replace it, Claude-Code style.
        First a free local step: stale tool outputs are stamped out of history;
        if that alone shrinks the context under the threshold, no model call is
        spent on a summarized transcript at all."""
        if self.compacting:
            return
        messages = self.messages
        n_elided = self._elide_stale_tools()
        if n_elided and not manual and not self.compaction_needed():
            self.on_event("compact_end", {"elided": n_elided}, {})
            self.save()
            return
        self.messages = messages  # elision stays in history either way
        split = self._turn_split(messages)
        # One very long agentic turn (100+ tool calls) can fill the window by itself. Then "the last 1-2
        # turns" is nearly everything: compacting would save ~nothing and re-trigger on the very next
        # step, over and over (each pass also invalidates the model server's prompt cache). Split inside
        # the turn instead, keeping only the newest messages.
        window = self.model_config.context_window
        in_turn = False
        if self._msgs_tokens(messages[split:]) > window * 0.45:
            inner = self._split_by_budget(messages, int(window * 0.3))
            if inner > split:
                split, in_turn = inner, True
        if split == 0:
            if not manual:
                return
            split = len(messages)  # forced: compact even without a clean boundary
        head, tail = messages[:split], messages[split:]
        if not head:
            self.on_event("compact_end", {}, {})
            return
        self.compacting = True
        self.on_event("compact_start")
        before = self.last_usage.get("prompt_tokens") or self.estimate_tokens()
        transcript = self._transcript(head, budget_chars=int(window * 0.6) * 4)
        out_parts = []
        failure = None
        try:
            for mc, prov in self._compaction_models(provider):
                out_parts.clear()
                try:
                    if mc is not self.model_config:
                        self.set_active_model(mc, "summarizing")
                    stream = prov.stream(
                        [{"role": "user", "content": transcript}], COMPACT_PROMPT, [],
                        model=mc.model, max_output_tokens=min(mc.max_output_tokens, COMPACT_MAX_TOKENS),
                        extra_body=mc.extra_body, timeout=self.settings.request_timeout)
                    for event, *payload in self._interruptible(stream, prov):
                        if event == "text":
                            out_parts.append(payload[0])
                        elif event == "done" and payload and payload[0]:
                            self.record_usage(mc.model, payload[0])
                    failure = None
                    break
                except (llm.ProviderError, httpx.HTTPError) as exc:
                    failure = exc
                    if mc is not self.model_config:        # the faster model failed: try the session's own
                        self.on_event("note", f"⚠ could not summarize with {mc.name} ({str(exc)[:120]}) — "
                                              f"using {self.model_config.name} instead")
        except BaseException:
            self.compacting = False
            raise
        finally:
            if self.active_model:
                self.active_model = ""
                self.on_event("model_active")
        if failure is not None:
            self.compacting = False
            self.messages = messages  # uncompacted
            self.on_event("provider_error", f"compaction failed: {failure}")
            return
        if self._stop.is_set():       # cancelled mid-summary: keep the history as it was, never a partial summary
            self.compacting = False
            self.messages = messages
            return
        summary = "".join(out_parts).strip()
        try:   # durable facts become project memory — no extra model call
            from . import memory as memory_mod
            memory_mod.save_auto_facts(self.cwd, memory_mod.parse_durable_facts(summary))
        except Exception:  # noqa: BLE001
            pass
        request = ""
        if in_turn:   # the user's message that started this turn is in the summarized part: keep it verbatim
            asked = next((m for m in reversed(head) if m["role"] == "user"
                          and not str(m.get("content", "")).startswith("<auto-compaction>")), None)
            if asked:
                request = f"\n\nThe user's current request, verbatim:\n{str(asked.get('content', ''))[:4000]}"
        compacted: list[dict] = [{"role": "user", "content":
                                  f"<auto-compaction>\n\n{summary}{request}\n</auto-compaction>\n\n"
                                  + ON_COMPACTION_NOTE}]
        self.messages = compacted + tail
        self.last_usage = {}
        self.compacting = False
        after = self.estimate_tokens()
        self.on_event("compact_end", {"before": before, "after": after,
                                      "dropped": len(head), "kept": len(tail)})
        self.save()

    def _turn_split(self, messages: list[dict]) -> int:
        """Index where the compacted tail begins: the last 1–2 real user turns."""
        user_idx = [i for i, m in enumerate(messages) if m["role"] == "user"]
        if not user_idx:
            return 0
        last = user_idx[-1]
        if len(user_idx) >= 2 and (last - user_idx[-2]) <= 12:
            return user_idx[-2]
        return last

    def _compaction_models(self, provider) -> list:
        """(model profile, provider) pairs to try, in order: the compact_profile model (usually a fast cloud model,
        so a summary takes seconds instead of many minutes on slow local hardware), then this session's own."""
        pairs = []
        name = getattr(self.settings, "compact_profile", "")
        if name and name != self.model_config.name:
            from .config import load_config
            try:
                mc = load_config().model(name)
                pairs.append((mc, llm.make_provider(mc, self.settings)))
            except (KeyError, ValueError, SystemExit):     # unknown profile or no API key: just use our own model
                self.on_event("note", f"⚠ compact_profile {name!r} is not usable (unknown profile or missing API key) "
                                      "— summarizing with the current model")
        pairs.append((self.model_config, provider))
        return pairs

    def _split_by_budget(self, messages: list[dict], budget: int) -> int:
        """Index where a tail worth about `budget` tokens begins, starting on an assistant message so a
        tool result is never separated from the call that produced it. 0 when there is no such point."""
        used = 0
        start = len(messages)
        while start > 0 and used + self._msgs_tokens(messages[start - 1:start]) <= budget:
            start -= 1
            used += self._msgs_tokens(messages[start:start + 1])
        while start < len(messages) and messages[start]["role"] != "assistant":
            start += 1
        return start if 0 < start < len(messages) else 0

    def _transcript(self, head: list[dict], budget_chars: int | None = None) -> str:
        lines = ["Below is a conversation transcript. Summarize it per the instructions."]
        tool_clip = 4000
        if budget_chars:     # many tool results must still fit in the summarizer's own context window
            tool_clip = max(300, min(4000, budget_chars // max(1, len(head))))
        for msg in head:
            role = msg["role"]
            if role == "tool":
                lines.append(f"[tool result] {msg.get('content', '')[:tool_clip]}")
            elif msg.get("tool_calls"):
                for call in msg["tool_calls"]:
                    lines.append(f"[assistant -> {call['name']}] "
                                 f"{json.dumps(call['arguments'])[:1500]}")
                if msg.get("content", "").strip():
                    lines.append(f"[assistant] {msg['content']}")
            else:
                body = f"[{role}] {msg.get('content', '')}"
                if role == "assistant":
                    body = body[:8000]
                lines.append(body)
        return COMPACT_PROMPT + "\n\n" + "\n".join(lines[:400])

    # ---------------------------------------------------------------- persistence

    def save(self) -> None:
        sessions = Path(home_data_dir() / "sessions")
        try:
            sessions.mkdir(parents=True, exist_ok=True)
            payload = {
                "id": self.id, "created": self.created, "cwd": self.cwd,
                "model": (getattr(self, "_escalated_from", None) or self.model_config).name,
                "messages": self.messages[-400:],
            }
            (sessions / f"{self.id}.json").write_text(json.dumps(payload))
        except OSError:
            pass

    @classmethod
    def resume_latest(cls, cwd: str | None = None) -> dict | None:
        """Most recent session — for `cwd`'s project when given, so `hexiel -c`
        in one repo never resumes a conversation from another."""
        try:
            sessions = sorted((home_data_dir() / "sessions").glob("*.json"),
                              key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return None
        legacy = None   # sessions saved before cwd was recorded
        for path in sessions:
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if cwd is None or data.get("cwd") == cwd:
                return data
            if legacy is None and "cwd" not in data:
                legacy = data
        return legacy