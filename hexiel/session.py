"""Session state: the agentic loop, token accounting, and auto-compaction."""

import json
import threading
import time
import uuid

import httpx
from collections.abc import Callable
from pathlib import Path

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

Be exhaustive on specifics (paths, flags, names, numbers) — this is not a chat summary,
it is the replacement for everything that gets dropped.
"""

ON_COMPACTION_NOTE = (
    "This session was auto-compacted: everything above this point is a summary of the "
    "earlier conversation. Its specifics (paths, decisions, promises) are authoritative. "
    "Continue the work described in Next Steps unless the user says otherwise."
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


class Session:
    def __init__(self, model_config: ModelConfig, settings: Settings, cwd: str,
                 tools: list, system_prompt: str, on_event: Callable = None,
                 on_approve: Callable = None):
        self.model_config = model_config
        self.settings = settings
        self.cwd = cwd
        self.tools = tools
        self.system_prompt = system_prompt
        self.on_event = on_event or (lambda *_a, **_k: None)
        self.on_approve = on_approve or (lambda *_: "yes")
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
        self.compacting = False
        self._stop = threading.Event()
        self._tool_ctx = None  # bind with bind_tool_ctx() before run_turn

    # ---------------------------------------------------------------- run turn

    def stop(self) -> None:
        self._stop.set()

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
        images, notes = attach_images(user_text, self.cwd)
        entry = {"role": "user", "content": "\n".join(
            [user_text, *notes, *([PLAN_MODE_NOTE] if self.plan_mode else [])])}
        if images:
            entry["images"] = images
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
                if self.compaction_needed() and not self.compacting:
                    self.compact(provider)
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
        while True:
            try:
                return self._stream_once(self._provider_override or provider, tool_defs)
            except llm.RateLimitError as exc:
                wait = self._limit_wait(exc, attempt)
                if wait is None:
                    if self._switch_to_fallback(str(exc)):
                        attempt = 0
                        continue
                    self._provider_error = str(exc) + (
                        "" if self.settings.auto_resume else " (auto-resume is off)")
                    return {"role": "assistant", "content": ""}
                self.on_event("rate_limited", wait, str(exc)[:200])
                deadline = time.time() + wait
                while time.time() < deadline:
                    if self._stop.is_set():
                        return {"role": "assistant", "content": ""}
                    time.sleep(min(1.0, deadline - time.time()))
                self.on_event("resumed")
                attempt += 1

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
        try:
            stream = provider.stream(self.messages, self.system_prompt, tool_defs,
                                     model=self.model_config.model,
                                     max_output_tokens=self.model_config.max_output_tokens,
                                     extra_body=self.model_config.extra_body,
                                     timeout=self.settings.request_timeout,
                                     echo_reasoning=self.model_config.echo_reasoning,
                                     max_tokens_param=self.model_config.max_tokens_param)
            for event, *payload in stream:
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
                raise llm.TransientError(
                    f"the connection to the model server failed ({type(exc).__name__}) — retrying") from exc
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
        if needs_ask:
            verdict = self.on_approve(tool.name, call["arguments"])
            if verdict == "always":
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

    # ---------------------------------------------------------------- token accounting

    def tokens_now(self) -> int:
        usage = self.last_usage.get("prompt_tokens") if self.last_usage else 0
        if usage:
            # usage is one turn stale if messages were appended since; add their estimate
            return usage
        return self.estimate_tokens()

    def estimate_tokens(self) -> int:
        total = llm.est_tokens(self.system_prompt) + 300  # tool schemas
        for msg in self.messages:
            total += llm.est_tokens(msg.get("content", "")) + 8
            for call in msg.get("tool_calls", []):
                total += llm.est_tokens(json.dumps(call.get("arguments", {}))) + 20
            for image in msg.get("images", []):
                total += image.get("approx_tokens", 800)
        return total

    def compaction_needed(self) -> bool:
        if not self.messages:
            return False
        floor = self.estimate_tokens()
        actual = self.last_usage.get("prompt_tokens", 0)
        current = max(floor, actual)
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
        transcript = self._transcript(head)
        out_parts = []
        try:
            stream = provider.stream(
                [{"role": "user", "content": transcript}], COMPACT_PROMPT, [],
                model=self.model_config.model,
                max_output_tokens=max(self.model_config.max_output_tokens, 8192),
                extra_body=self.model_config.extra_body,
                timeout=self.settings.request_timeout)
            for event, *payload in stream:
                if event == "text":
                    out_parts.append(payload[0])
        except llm.ProviderError as exc:
            self.compacting = False
            self.messages = messages  # uncompacted
            self.on_event("provider_error", f"compaction failed: {exc}")
            return
        except BaseException:
            self.compacting = False
            raise
        summary = "".join(out_parts).strip()
        try:   # durable facts become project memory — no extra model call
            from . import memory as memory_mod
            memory_mod.save_auto_facts(self.cwd, memory_mod.parse_durable_facts(summary))
        except Exception:  # noqa: BLE001
            pass
        compacted: list[dict] = [{"role": "user", "content":
                                  f"<auto-compaction>\n\n{summary}\n</auto-compaction>\n\n"
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

    def _transcript(self, head: list[dict]) -> str:
        lines = ["Below is a conversation transcript. Summarize it per the instructions."]
        for msg in head:
            role = msg["role"]
            if role == "tool":
                lines.append(f"[tool result] {msg.get('content', '')[:4000]}")
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
                "model": self.model_config.name, "messages": self.messages[-400:],
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