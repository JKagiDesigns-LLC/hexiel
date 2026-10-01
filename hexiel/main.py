"""hexiel CLI entry point: TUI, REPL, or one-shot (-p) modes."""

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from . import memory as memory_mod
from . import openwebui_compat
from . import prompt as prompt_mod
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
    ap.add_argument("--serve", action="store_true",
                    help="run the browser web UI instead of the TUI")
    ap.add_argument("--host", default="127.0.0.1", help="web UI bind host (default: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8777, help="web UI port (default: 8777)")
    ap.add_argument("--presets", action="store_true",
                    help="list ready-made model profiles for the major providers")
    ap.add_argument("--doctor", action="store_true",
                    help="run environment preflight checks and exit")
    ap.add_argument("--setup", action="store_true", help="first-run guided wizard")
    ap.add_argument("-V", "--version", action="version", version=f"hexiel {__version__}")
    return ap.parse_args()


def build_session(cwd: str, model_name: str | None, resumed: dict | None = None) -> Session:
    config = load_config(model_name)
    model_config = config.models.get(resumed["model"], config.model()) if resumed else config.model()
    settings = config.settings

    chain = detect_toolchain(cwd)
    skills = skills_mod.discover(cwd)
    tools = build_tools(chain)
    plugins = openwebui_compat.discover_plugins(cwd)
    tool_defs = openwebui_compat.plugin_tool_defs(plugins, cwd)
    from . import mcp_client
    mcp_servers = mcp_client.start_servers(mcp_client.load_configs(cwd, config.mcp))
    tool_defs = tool_defs + mcp_client.tool_defs(mcp_servers)
    if tool_defs:
        known = {t.name for t in tools}
        tools = tools + [t for t in tool_defs if t.name not in known]
    tool_ctx = ToolContext(cwd=cwd, chain=chain,
                           search_backend=settings.search_backend,
                           searxng_url=settings.searxng_url, skills=skills)

    system = prompt_mod.build_system_prompt(
        cwd, skills_mod.prompt_section(skills), toolchain_section(chain, cwd),
        task_state=(todo_mod.summary_line(cwd) if resumed else ""))
    session = Session(model_config, settings, cwd, tools, system)
    session.mcp_servers = mcp_servers
    from . import permissions
    session.rules = permissions.load(cwd, config.permissions)
    from .tools import ToolDef
    session.tools.append(ToolDef(
        "task", "Delegate a self-contained investigation (e.g. 'find where X is configured and how "
        "it is used', 'research Y on the web') to a subagent with its own context and read-only "
        "tools. Only its final answer is returned — use it for searches that would otherwise "
        "flood this conversation with file contents.",
        {"type": "object", "properties": {
            "description": {"type": "string", "description": "3-6 word label"},
            "prompt": {"type": "string", "description": "the full task, with everything it needs"}},
         "required": ["prompt"]},
        readonly=True, run=lambda args, ctx: session.run_subagent(args)))
    session.bind_tool_ctx(tool_ctx)
    # gitlab propose steps share the standard approval gate; "no" declines the step
    tool_ctx.on_step = (lambda kind, desc: session.on_approve(f"gitlab:{kind}",
                                                              {"step": desc}) != "no")
    if resumed:
        session.id = resumed["id"]
        session.messages = resumed.get("messages", [])
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


def repl_loop(session: Session) -> None:
    session.on_event = repl_event
    session.on_approve = terminal_approval
    print("hexiel REPL — empty line to exit, /clear resets context, /compact forces compaction")
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


def main() -> None:
    args = parse_args()
    cwd = str(Path(args.path).resolve())
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

    resumed = Session.resume_latest(cwd) if args.continue_ else None
    session = build_session(cwd, args.model, resumed)
    memory_mod.ensure_dirs(cwd)
    if args.yes:
        session.auto_approve = True

    if args.print_mode is not None:
        prompts = [args.print_mode]
    else:
        prompts = None

    if args.print_mode is not None:
        headless_loop(session, prompts)
    elif args.serve:
        try:
            from .webui import serve as webui_serve
        except ImportError as exc:
            raise SystemExit(f"web UI needs aiohttp:  pip install 'hexiel[serve]'  ({exc})")
        webui_serve(session, host=args.host, port=args.port)
    elif args.repl:
        repl_loop(session)
    else:
        from .tui import HexielTUI
        result = HexielTUI(session, skills_mod.discover(cwd),
                           skills_mod.command_names(skills_mod.discover(cwd))).run()
        if result == "restart":      # /update installed a new version
            from .updater import restart
            restart()


if __name__ == "__main__":
    main()