"""hexiel CLI entry point: TUI, REPL, or one-shot (-p) modes."""

import argparse
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from . import __version__
from . import memory as memory_mod
from . import openwebui_compat
from . import prompt as prompt_mod
from . import prompt_edit
from . import settings_edit
from . import skills as skills_mod
from . import todo as todo_mod
from .config import load_config
from .session import Session
from .tooling import Toolchain, detect_toolchain, toolchain_section
from .tools import ToolContext, build_tools


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="hexiel",
        description="Terminal coding agent with memory, skills, native toolchain "
                    "integration, and auto-compacting context.")
    ap.add_argument("path", nargs="?", default=".", help="working directory (default: here)")
    ap.add_argument("-m", "--model", help="model profile name from config.toml")
    ap.add_argument("-p", "--print", dest="print_mode", metavar="PROMPT",
                    help="run one prompt headlessly, then exit")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="auto-approve all tool calls (headless/scripted runs)")
    ap.add_argument("-c", "--continue", dest="continue_", action="store_true",
                    help="resume the most recent session")
    ap.add_argument("--repl", action="store_true", help="plain readline loop (no TUI)")
    ap.add_argument("--worktree", action="store_true",
                    help="run in a fresh git worktree of the directory (parallel runs never collide; "
                         "untracked .env-style files are copied in; merge or remove the worktree after)")
    ap.add_argument("--ci", nargs="?", const="", metavar="NOTES",
                    help="CI runner mode: review the pipeline's MR or commit (GitLab env), read-only, "
                         "findings posted back to the MR/commit when a token is set; optional notes "
                         "are appended to the prompt (e.g. --ci 'focus on the payment module')")
    ap.add_argument("--serve", action="store_true",
                    help="run the browser web UI instead of the TUI")
    ap.add_argument("--remote", action="store_true",
                    help="with --serve: also connect to Hexiel Remote (paid; `hexiel remote login` first)")
    ap.add_argument("--host", default="127.0.0.1", help="web UI bind host (default: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8777, help="web UI port (default: 8777)")
    ap.add_argument("--presets", action="store_true",
                    help="list ready-made model profiles for the major providers")
    ap.add_argument("--doctor", action="store_true",
                    help="run environment preflight checks and exit")
    ap.add_argument("--setup", action="store_true", help="first-run guided wizard")
    ap.add_argument("-V", "--version", action="version", version=f"hexiel {__version__}")
    return ap.parse_args()


def build_session(cwd: str, model_name: str | None, resumed: dict | None = None,
                  reuse: Session | None = None, readonly_only: bool = False) -> Session:
    config = load_config(model_name)
    model_config = config.models.get(resumed["model"], config.model()) if resumed else config.model()
    settings = config.settings

    chain = detect_toolchain(cwd)
    skills = skills_mod.discover(cwd)
    tools = build_tools(chain)
    plugins = openwebui_compat.discover_plugins(cwd)
    tool_defs = openwebui_compat.plugin_tool_defs(plugins, cwd)
    from . import mcp_client
    mcp_servers = getattr(reuse, "mcp_servers", None) or mcp_client.start_servers(
        mcp_client.load_configs(cwd, config.mcp))   # reuse=an in-process session (/fork): never double-start
    tool_defs = tool_defs + mcp_client.tool_defs(mcp_servers)
    if tool_defs:
        known = {t.name for t in tools}
        tools = tools + [t for t in tool_defs if t.name not in known]
    from . import custom_tools                    # tools hexiel made for itself (.hexiel/tools/*.py)
    known = {t.name for t in tools}
    tools = tools + [t for t in custom_tools.discover(cwd) if t.name not in known]
    if readonly_only:                             # CI runner mode: investigate and report, never change files
        tools = [t for t in tools if t.readonly]
    custom_tools._fingerprints[cwd] = custom_tools.fingerprint(cwd)   # boot load counts as synced
    tool_ctx = ToolContext(cwd=cwd, chain=chain,
                           search_backend=settings.search_backend,
                           searxng_url=settings.searxng_url, skills=skills,
                           test_commands=tuple(settings.test_commands), tools=tools)

    system = prompt_mod.build_system_prompt(
        cwd, skills_mod.prompt_section(skills), toolchain_section(chain, cwd),
        task_state=(todo_mod.summary_line(cwd) if resumed else ""))
    session = Session(model_config, settings, cwd, tools, system)
    prompt_edit.startup(session, resumed)          # an edited /prompt (session copy, dir or global file) takes over
    session.mcp_servers = mcp_servers
    from . import permissions
    session.rules = permissions.load(cwd, config.permissions)
    from .tools import ToolDef
    from . import toolsets
    toolsets.install(session, config.settings)
    session.tools.append(ToolDef(
        "task", "Delegate a self-contained investigation (e.g. 'find where X is configured and how "
        "it is used', 'research Y on the web') to a subagent with its own context and read-only "
        "tools, or mode=\"write\" for a full implementation job: write clones its own git worktree, "
        "gets every tool, runs/commits there autonomously (approval-free inside the box) and only "
        "its final answer is returned — use task for searches that would otherwise flood this "
        "conversation with file contents, and fan out several in one reply to run them in parallel.",
        {"type": "object", "properties": {
            "description": {"type": "string", "description": "3-6 word label"},
            "prompt": {"type": "string", "description": "the full task, with everything it needs"},
            "mode": {"type": "string", "enum": ["research", "write"],
                     "description": "research (default): read-only helper, the answer only. write: the "
                                    "helper implements, tests and commits inside its own git worktree; "
                                    "the answer reports the branch and the merge command"},
            "worktree": {"type": "boolean",
                         "description": "run in its own git worktree (safe alongside other runs; "
                                        "the answer reports the path and merge hint) — write mode "
                                        "always gets one, named after the description unless given"}},
        "required": ["prompt"]},
        readonly=True, run=lambda args, ctx: session.run_subagent(args)))
    if settings.consult_mode != "off" and (settings.planner_profile or settings.fallback_profile):
        from . import consult
        session.tools.append(consult.tool_def(session))       # the working model can ask the strong model for advice
    from . import timers
    session.tools.append(timers.tool_def(session))            # countdowns the user sees for long waits
    session.bind_tool_ctx(tool_ctx)
    # gitlab propose steps share the standard approval gate; "no" declines the step
    tool_ctx.on_step = (lambda kind, desc: session.on_approve(f"gitlab:{kind}",
                                                              {"step": desc}) != "no")
    if resumed:
        session.id = resumed["id"]
        session.messages = resumed.get("messages", [])
        from . import redact
        if getattr(settings, "redact_secrets", "full") != "off":
            # sessions saved before scrubbing existed still hold secrets in old messages: clean them
            # once as they load (idempotent — already-scrubbed text comes back unchanged)
            for m in session.messages:
                if isinstance(m.get("content"), str):
                    m["content"] = redact.scrub(m["content"])
                for block_kind in ("tool_calls",):
                    for c in m.get(block_kind) or []:
                        if isinstance(c.get("arguments"), dict):
                            for k, v in c["arguments"].items():
                                if isinstance(v, str):
                                    c["arguments"][k] = redact.scrub(v)
        session.timers = [t for t in resumed.get("timers", []) if isinstance(t, dict) and "ends" in t]
        settings_edit.restore_auto_mode(session, resumed)
    if readonly_only:                             # late appends (toolsets, consult) are filtered too
        session.tools = [t for t in session.tools if t.readonly]
    return session


def terminal_approval(tool_name: str, args: dict) -> str:
    shown = str(args)[:600]
    while True:
        sys.stderr.write(f"\nhexiel: allow {tool_name}? {shown}\n[y]es / [a]lways / [n]o: ")
        sys.stderr.flush()
        try:
            answer = input().strip().lower()[:1]
        except EOFError:
            return "no"
        if answer in ("y", "a", "n"):
            return {"y": "yes", "a": "always", "n": "no"}[answer]


def headless_loop(session: Session, prompts: list[str]) -> None:
    thinking = {"chars": 0}

    def on_event(event, *payload, **_kw) -> None:
        if event == "thinking":
            # progress on stderr so reasoning models don't look hung; stdout stays the answer
            before = thinking["chars"] // 2000
            thinking["chars"] += len(payload[0])
            if thinking["chars"] // 2000 > before or before == 0 and thinking["chars"] == len(payload[0]):
                sys.stderr.write(f"\r(thinking… ~{thinking['chars'] // 4} tokens)")
                sys.stderr.flush()
            return
        if event == "rate_limited":
            sys.stderr.write(f"\n[rate limited — resuming automatically in {int(payload[0])}s]\n")
            sys.stderr.flush()
            return
        if event == "model_unreachable":
            sys.stderr.write(f"\n[model server not responding — retrying in {int(payload[0])}s: {payload[1]}]\n")
            sys.stderr.flush()
            return
        if event == "note":
            sys.stderr.write(f"\n{payload[0]}\n")
            sys.stderr.flush()
            return
        if event == "fallback":
            sys.stderr.write(f"\n[{payload[0]} limited — continuing on fallback {payload[1]}]\n")
            return
        if thinking["chars"] and event in ("text", "tool_call", "turn_end"):
            sys.stderr.write("\n")
            thinking["chars"] = 0
        if event == "text":
            sys.stdout.write(payload[0])
            sys.stdout.flush()
        elif event == "turn_end" and payload and isinstance(payload[0], dict) and payload[0].get("error"):
            sys.stderr.write(f"\nhexiel: provider error: {payload[0]['error']}\n")
        elif event == "compact_end" and payload and payload[0]:
            stats = payload[0]
            sys.stderr.write(f"\n[compacted {stats.get('before', 0) // 1000}k → "
                             f"{stats.get('after', 0) // 1000}k tokens]\n")
    session.on_event = on_event
    session.on_approve = terminal_approval
    for text in prompts:
        if text:
            sys.stderr.write(f"❯ {text}\n")
            session.run_turn(text)
    sys.stdout.write("\n")
    if session.usage_by_model:       # one line a script (or the benchmark) can read: tokens per model
        sys.stderr.write("HEXIEL_USAGE " + json.dumps(session.usage_by_model) + "\n")
    if os.environ.get("HEXIEL_CI_REPORT"):  # --ci: post the findings where the pipeline wants them
        from . import ci_mode
        ctx = ci_mode.ci_context()
        answer = next((m.get("content", "") for m in reversed(session.messages)
                       if m.get("role") == "assistant" and m.get("content")), "")
        if answer:
            sys.stderr.write(f"[ci] {ci_mode.report(ctx, answer)}\n")


def _repl_completer(session):
    """readline completer for --repl: slash commands, skills and file paths (Tab)."""
    from .complete import completions
    cwd = session.cwd

    def guide(text: str) -> list[str]:
        from . import commands as commands_mod
        if text.startswith("/"):
            names = [n for n, _, _ in commands_mod.BUILTIN if n.startswith(text[1:].lower())]
            names += list(commands_mod.discover(cwd))
            return sorted(set(names))
        if not text:
            return []
        paths = []
        for key in (text, f"./{text}") if not text.startswith(("./", "/", "~")) else (text,):
            for comp in (completions(key, cwd) or []):
                paths.append(comp["name"])
        return sorted(set(paths))

    def build(text: str, state: int) -> str | None:
        rows = guide(text)
        return (f"/{rows[state]}" if text.startswith("/") else rows[state]) \
            if state < len(rows) else None
    return build


def repl_loop(session: Session) -> None:
    session.on_event = repl_event
    session.on_approve = terminal_approval
    print("hexiel REPL — empty line to exit, /clear resets context, /compact forces compaction")
    try:                                       # best-effort Tab completion (slash cmds + paths)
        import readline
        readline.set_completer(_repl_completer(session))
        readline.set_completer_delims(" \t\n'\"")
        readline.parse_and_bind("tab: complete")
    except ImportError:
        pass
    while True:
        try:
            text = input("❯ ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not text:
            return
        if text.startswith("/"):
            if text == "/clear":
                session.messages = []
                session.last_usage = {}
                print("(context cleared)")
                continue
            if text == "/compact":
                from .llm import make_provider
                provider = make_provider(session.model_config, session.settings)
                session.compact(provider, manual=True)
                continue
            print(f"unknown command {text} (REPL supports /clear /compact)")
            continue
        session.run_turn(text)
        print("\n")


def repl_event(event, *payload) -> None:
    if event == "text":
        sys.stdout.write(payload[0])
        sys.stdout.flush()
    elif event == "tool_call":
        call = payload[0]
        args = call.get("arguments", {})
        first = next((str(v) for v in args.values() if v), "")
        print(f"\n  {call['name']}({first[:60]})")
    elif event == "compact_end":
        stats = payload[0] if payload else {}
        if stats:
            print(f"  [compacted {stats.get('before', 0) // 1000}k -> "
                  f"{stats.get('after', 0) // 1000}k tokens]")


def jobs_cli(argv: list[str]) -> int:
    """hexiel jobs list | run <id> | remove <id> | tick | install"""
    from . import jobs
    cmd = argv[0] if argv else "list"
    if cmd == "tick":
        jobs.tick()
        return 0
    if cmd == "install":
        print(jobs.install())
        return 0
    if cmd == "run" and len(argv) > 1:
        job = next((j for j in jobs.load() if argv[1] in (j["id"], j["name"])), None)
        if not job:
            print("no such job")
            return 1
        ok, answer = jobs.run_job(job)
        print(answer)
        print(f"\n(delivered to {jobs.deliver(job, ok, answer)})")
        return 0 if ok else 1
    if cmd == "remove" and len(argv) > 1:
        print("removed" if jobs.remove(argv[1]) else "no such job")
        return 0
    items = jobs.load()
    print("\n".join(jobs.describe(j) for j in items) or "no scheduled jobs — ask hexiel to schedule one")
    print(f"\nscheduler {'installed' if jobs.installed() else 'not installed (hexiel jobs install)'}"
          f" · inbox: {jobs.inbox_dir()}")
    return 0


def main() -> None:
    from .remote import ensure_streams
    ensure_streams()                  # no console (Windows scheduled task): log to a file instead of crashing
    from .remote import load_service_env
    load_service_env()                # keys recorded by `hexiel remote install` (a service doesn't see your shell profile)
    if len(sys.argv) > 1 and sys.argv[1] in ("knowledge", "rag"):
        from .knowledge import cli as knowledge_cli
        raise SystemExit(knowledge_cli(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "jobs":
        raise SystemExit(jobs_cli(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] in ("instructions", "import-agents"):
        from .instructions import cli as instructions_cli
        raise SystemExit(instructions_cli(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "remote":
        from .remote import remote_cli
        raise SystemExit(remote_cli(sys.argv[2:]))
    args = parse_args()
    cwd = str(Path(args.path).resolve())
    if getattr(args, "worktree", False):
        try:
            from . import worktree as wt_mod
            if not wt_mod.is_repo(cwd):
                raise SystemExit(f"hexiel: --worktree needs a git repository: {cwd}")
            w = wt_mod.create(cwd, "")
            cwd = str(w.path)
            print(f"▸ isolated in worktree {w.path} (branch {w.branch or '(detached)'}) — "
                  f"merge with `git merge {w.branch}` after review", file=sys.stderr)
        except SystemExit:
            raise
        except (subprocess.CalledProcessError, OSError) as exc:   # git (or the fs) said why
            detail = getattr(exc, "stderr", "") or str(exc)
            raise SystemExit(f"hexiel: could not create a worktree: {str(detail).strip()[:200]}")
    if args.serve and args.remote:
        from .remote import is_service_invocation, remote_cli
        if is_service_invocation(cwd, args.port):      # a service installed before 0.4.7: run automations only
            raise SystemExit(remote_cli(["run"]))
    os.chdir(cwd)

    if args.presets:
        from .presets import listing
        print(listing())
        return
    if args.doctor:
        from .doctor import doctor_main
        raise SystemExit(doctor_main(cwd))
    if args.setup:
        from .doctor import wizard
        wizard(cwd)
        return

    if args.print_mode is None and not args.serve and sys.stdin.isatty() and sys.stdout.isatty():
        from .instructions import offer_import
        try:
            offer_import(cwd)                 # one-time, prompted; declining is remembered
        except (EOFError, KeyboardInterrupt, OSError):
            pass
    resumed = Session.resume_latest(cwd) if args.continue_ else None
    ci_ = None
    if args.ci is not None:                       # --ci: headless review run (None = flag absent)
        from . import ci_mode
        ci_ = ci_mode.ci_context()
        os.environ["HEXIEL_CI_REPORT"] = "1"      # headless_loop reports the findings back after the turn
    try:
        session = build_session(cwd, args.model, resumed, readonly_only=bool(ci_ is not None and not args.yes))
    except ValueError as exc:                 # e.g. `hexiel -m typo`: say so instead of a traceback
        raise SystemExit(f"hexiel: {exc}")
    memory_mod.ensure_dirs(cwd)
    from . import reporter
    if reporter.mode() != "off":
        reporter.in_background(reporter.flush_outbox)      # reports queued while offline
    from . import remote
    remote.resume_if_orphaned()                           # a killed /remote-control chat (before 0.4.7) left it paused
    from . import telemetry as telemetry_mod
    telemetry_mod.ping()                                  # anonymous install/upgrade ping for the website counter
    from . import llm as llm_mod
    if session.model_config.keep_alive:                   # local models: pre-warm so the first real
        threading.Thread(                                 # message doesn't pay the 30–120s reload
            target=llm_mod.warm_up, args=(session.model_config,), daemon=True).start()
    if args.yes:
        session.auto_approve = True
        session.guard_commits = False          # -y is "everything"; use auto_approve = "except-commits" for the careful mode

    if args.print_mode is not None:
        prompts = [args.print_mode]
    elif ci_ is not None:                         # --ci implies a headless run: build the prompt
        from . import ci_mode
        prompts = [ci_mode.prompt_for(ci_, cwd, args.ci or "")]
    else:
        prompts = None

    if args.print_mode is not None or ci_ is not None:
        headless_loop(session, prompts)
    elif args.serve:
        try:
            from .webui import serve as webui_serve
        except ImportError as exc:
            raise SystemExit(f"web UI needs aiohttp:  pip install 'hexiel[serve]'  ({exc})")
        if args.remote:
            import asyncio
            from .remote import serve_with_remote
            try:
                asyncio.run(serve_with_remote(session, args.host, args.port))
            except KeyboardInterrupt:
                pass
        else:
            webui_serve(session, host=args.host, port=args.port)
    elif args.repl:
        repl_loop(session)
    else:
        from .tui import HexielTUI, prepare_terminal_graphics
        prepare_terminal_graphics()   # probe TGP/kitty support before stdin is owned + quiet acks
        result = HexielTUI(session, skills_mod.discover(cwd),
                           skills_mod.command_names(skills_mod.discover(cwd))).run()
        if result == "restart":      # /update installed a new version
            from .updater import restart
            restart()


if __name__ == "__main__":
    main()