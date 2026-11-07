"""Ticket evidence screenshots for the 0.8.7 isolation+accuracy batch: worktrees (/wt),
LSP diagnostics, formatter integration, sandboxing, CI runner mode."""
import asyncio
import os
import subprocess
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("HEXIEL_DATA", "/tmp/opencode/ssdata")
os.environ.setdefault("HEXIEL_HOME", "/tmp/opencode/sshome")
os.environ["HEXIEL_NO_REPORTS"] = "1"


def _mk(messages: list[dict], cwd: str):
    from hexiel.config import ModelConfig, Settings
    from hexiel.session import Session
    session = Session(ModelConfig(name="gmktec", provider="openai", model="qwen3.6:35b-a3b-coding",
                                  base_url="http://127.0.0.1:11434/v1", context_window=200_000),
                      Settings(), cwd=cwd, tools=[], system_prompt="t")
    session.messages = messages
    return session


def _repo() -> Path:
    project = Path("/tmp/opencode/ssdata/wtproj")
    project.mkdir(parents=True, exist_ok=True)
    if not (project / ".git").exists():
        subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=project, check=True)
        (project / ".env").write_text("DB=local\n")
        (project / "app.py").write_text("def deploy():\n    return 'ok'\n")
        subprocess.run(["git", "add", "app.py"], cwd=project, check=True)
        subprocess.run(["git", "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-qm", "one"],
                       cwd=project, check=True)
    return project


def _ui_texts(app) -> str:
    return "\n".join(str(getattr(w, "visual", getattr(w, "source", ""))) for w in app.chat.children)


async def diagnostics_shot(out: Path) -> None:
    """#38: a write lands with a 🩺 diagnostics note (live ruff run)."""
    project = Path("/tmp/opencode/ssdata/diagproj")
    project.mkdir(parents=True, exist_ok=True)
    bad = project / "payments.py"
    bad.write_text("def total(lines):\n    total = sum(lines)\n    print(")

    from hexiel.config import ModelConfig, Settings
    from hexiel.session import Session
    from hexiel.tooling import Toolchain
    from hexiel.tools import ToolContext, build_tools
    tools = build_tools(Toolchain())
    session = Session(ModelConfig(name="gmktec", provider="openai", model="qwen3.6:35b-a3b-coding",
                                  base_url="http://127.0.0.1:11434/v1", context_window=200_000),
                      Settings(), cwd=str(project), tools=tools, system_prompt="t")
    session.bind_tool_ctx(ToolContext(cwd=str(project), chain=Toolchain()))
    session.auto_approve = True
    session._execute_tool({"id": "w1", "name": "write",
                           "arguments": {"path": str(bad), "content": "def total(lines):\n"
                                         "    total = sum(lines)\n    print(UNDEFINED_NAME)\n"}})
    result = types.SimpleNamespace(text=next((m["content"] for m in reversed(session.messages)
                                              if m.get("role") == "tool" and m.get("tool_call_id") == "w1"), ""))
    from hexiel.tui import HexielTUI
    app = HexielTUI(session, [], set())
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause(0.4)
        app.handle_event("tool_call", {"id": "w1", "name": "write",
                                       "arguments": {"path": "payments.py", "content": "…"}})
        app.handle_event("tool_result", {"id": "w1", "name": "write",
                                         "arguments": {"path": "payments.py", "content": "…"}},
                         result.text, False, 0.2)
        app.handle_event("text", "Payments module updated — one diagnostics note came back with the write.")
        await pilot.pause(0.6)
        out.write_text(app.export_screenshot())


async def worktree_shot(out: Path) -> None:
    """/wt: create a worktree for a parallel run, then list it."""
    project = _repo()
    from hexiel.tui import HexielTUI
    session = _mk([
        {"role": "user", "content": "start a refactor branch without touching my checkout", "ts": 0},
        {"role": "assistant", "content": "I'll give that work a git worktree of its own.", "ts": 0},
    ], cwd=str(project))
    app = HexielTUI(session, [], set())
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause(0.4)
        app._start_turn = lambda prompt: None           # no real model call in the demo
        app.cmd_wt("create parallel-refactor")
        await pilot.pause(0.4)
        app.cmd_wt("list")
        await pilot.pause(0.5)
        out.write_text(app.export_screenshot())


def export(name: str, coro_factory) -> None:
    out_dir = Path("/tmp/opencode/tickets")
    out_dir.mkdir(parents=True, exist_ok=True)
    svg_path = out_dir / f"{name}.svg"
    asyncio.run(coro_factory(svg_path))
    png = out_dir / f"{name}.png"
    subprocess.run(["magick", "-density", "144", str(svg_path), "-background", "#12141a",
                    "-resize", "1440x", str(png)], check=True, timeout=120)
    print(f"{png} ({png.stat().st_size // 1024} KB)")


def main(argv: list[str]) -> None:
    shots = {
        "worktree": worktree_shot,
        "diagnostics": diagnostics_shot,
    }
    for name in argv or list(shots):
        if name in shots:
            export(name, shots[name])


if __name__ == "__main__":
    main(sys.argv[1:])
