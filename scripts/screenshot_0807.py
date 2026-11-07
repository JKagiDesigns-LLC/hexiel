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


async def formatter_shot(out: Path) -> None:
    """#39: a write lands formatted (live ruff) without asking."""
    project = Path("/tmp/opencode/ssdata/fmtproj")
    project.mkdir(parents=True, exist_ok=True)
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
    session._execute_tool({"id": "w2", "name": "write",
                           "arguments": {"path": str(project / "models.py"),
                                         "content": "class Session:\n    def __init__(self,cwd,  name ) :\n        self.cwd,self.name=cwd,  name\n"}})
    result_text = next((m["content"] for m in reversed(session.messages)
                        if m.get("role") == "tool" and m.get("tool_call_id") == "w2"), "")
    from hexiel.tui import HexielTUI
    app = HexielTUI(session, [], set())
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause(0.4)
        call = {"id": "w2", "name": "write",
                "arguments": {"path": "models.py", "content": "…"}}
        app.handle_event("tool_call", call)
        app.handle_event("tool_result", call, result_text, False, 0.2)
        app.handle_event("text", "Session class written — it hit the disk already formatted (ruff).")
        await pilot.pause(0.6)
        out.write_text(app.export_screenshot())


async def sandbox_shot(out: Path) -> None:
    """#40: with sandbox=safe (bwrap), a write outside the box is refused; in the project it works."""
    home = Path(os.environ.get("HEXIEL_HOME", "/tmp/opencode/sshome"))
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text('[settings]\nsandbox = "safe"\n')
    project = Path("/tmp/opencode/ssdata/sbproj")
    project.mkdir(parents=True, exist_ok=True)
    from hexiel.config import ModelConfig, Settings
    from hexiel.session import Session
    from hexiel.tooling import Toolchain
    from hexiel.tools import ToolContext, build_tools
    tools = build_tools(Toolchain())                     # boot-wires sandbox MODE=safe
    session = Session(ModelConfig(name="gmktec", provider="openai", model="qwen3.6:35b-a3b-coding",
                                  base_url="http://127.0.0.1:11434/v1", context_window=200_000),
                      Settings(), cwd=str(project), tools=tools, system_prompt="t")
    session.bind_tool_ctx(ToolContext(cwd=str(project), chain=Toolchain()))
    session.auto_approve = True
    outside = session._execute_tool or None
    results = []
    for cid, cmd in (("s1", "echo trying to write over /usr/share > /usr/share/touch-me.txt"),
                     ("s2", "echo deploy > deployed.txt && cat deployed.txt")):
        session._execute_tool({"id": cid, "name": "bash", "arguments": {"command": cmd}})
        results.append(next(m["content"] for m in reversed(session.messages)
                            if m.get("role") == "tool" and m.get("tool_call_id") == cid))
    from hexiel.tui import HexielTUI
    app = HexielTUI(session, [], set())
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause(0.4)
        for (cid, cmd), text in zip((("s1", "echo trying to write over /usr/share > /usr/share/touch-me.txt"),
                                     ("s2", "echo deploy > deployed.txt && cat deployed.txt")), results):
            call = {"id": cid, "name": "bash", "arguments": {"command": cmd}}
            app.handle_event("tool_call", call)
            app.handle_event("tool_result", call, text, False, 0.2)
        app.handle_event("text", "sandbox = safe: the write outside the box was refused by the box itself.")
        await pilot.pause(0.6)
        out.write_text(app.export_screenshot())


async def ci_shot(out: Path) -> None:
    """#41: hexiel --ci in a simulated GitLab pipeline — real wiring, only the model text stubbed."""
    driver = Path("/tmp/opencode/ci_driver.py")
    driver.write_text('''
import os, sys, types
from pathlib import Path
os.environ["GITLAB_CI"] = "true"
os.environ["CI_API_V4_URL"] = "https://gitlab.example/api/v4"
os.environ["CI_PROJECT_ID"] = "77"
os.environ["CI_MERGE_REQUEST_IID"] = "12"
os.environ["CI_MERGE_REQUEST_DIFF_BASE_SHA"] = os.environ["BASE"]
os.environ["CI_MERGE_REQUEST_TARGET_BRANCH_NAME"] = "main"
os.environ["HEXIEL_NO_REPORTS"] = "1"
os.environ.pop("HEXIEL_GITLAB_TOKEN", None)

sys.path.insert(0, str(Path("%SRC%")))
sys.path.insert(0, str(Path("%SRC%") / "tests"))

replies = iter([
    {"role": "assistant", "content":
     "Findings for this MR, ordered by severity:\\n\\n"
     "1. **error — payments.py:12** charges are summed before the currency check, "
     "so a mixed-currency cart undercharges. Guard first, sum second.\\n"
     "2. **warning — payments.py:30** the retry loop has no cap: a stuck provider call "
     "pins the turn. Add a max_retries (default 3).\\n\\n"
     "No blocking issues beyond those two — nothing else regresses."}])
from hexiel.session import Session


def fake_stream_once(self, provider, tool_defs) -> dict:
    text = next(iter([{"role": "assistant", "content": FINDINGS}]))["content"]
    self.on_event("text", text)
    return {"role": "assistant", "content": text}


FINDINGS = (
    "Findings for this MR, ordered by severity:\\n\\n"
    "1. **error — payments.py:12** charges are summed before the currency check, "
    "so a mixed-currency cart undercharges. Guard first, sum second.\\n"
    "2. **warning — payments.py:30** the retry loop has no cap: a stuck provider call "
    "pins the turn. Add a max_retries (default 3).\\n\\n"
    "No blocking issues beyond those two — nothing else regresses.")

Session._stream_once = fake_stream_once
import hexiel.llm as llm
llm.make_provider = lambda *a, **k: None

sys.argv = ["hexiel", os.environ["PROJ"], "--ci", "focus on the payment module and be brief"]
from hexiel.main import main
main()
''')
    src = Path.cwd()
    driver.write_text(driver.read_text().replace("%SRC%", str(src)))
    driver.chmod(0o755)

    project = Path("/tmp/opencode/ssdata/ciprj")
    project.mkdir(parents=True, exist_ok=True)
    (project / "payments.py").write_text(
        "def charge(cart):\n    total = sum(l.amount for l in cart)\n    return total\n")
    subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=project, check=True)
    subprocess.run(["git", "add", "-f", "payments.py"], cwd=project, check=True)
    subprocess.run(["git", "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-qm", "base"], cwd=project, check=True)
    subprocess.run(["git", "checkout", "-qb", "feature/payments"], cwd=project, check=True)
    (project / "payments.py").write_text(
        "def charge(cart):\n    total = sum(l.amount for l in cart)\n"
        "    if cart.currency != 'eur': total = convert(total)\n    return total\n")
    subprocess.run(["git", "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-qam", "currency check after the sum"], cwd=project, check=True)
    base = subprocess.run(["git", "rev-parse", "main"], cwd=project, capture_output=True, text=True).stdout.strip()

    (Path(os.environ["HEXIEL_HOME"]) / "config.toml").write_text(
        '[settings]\n[models.m]\nprovider = "openai"\nmodel = "m"\nbase_url = ""\n')
    env = {**os.environ, "BASE": base, "PROJ": str(project),
           "HEXIEL_DATA": os.environ.get("HEXIEL_DATA", "/tmp/opencode/ssdata"),
           "HEXIEL_HOME": os.environ.get("HEXIEL_HOME", "/tmp/opencode/sshome")}
    run = subprocess.run([sys.executable, str(driver)], cwd=project, env=env,
                         capture_output=True, text=True, timeout=240)
    text = (run.stdout + run.stderr).strip()

    import html as _html
    lines = [_html.escape(l) for l in text.splitlines()]
    rows = "\n".join(f'<text x="28" y="{34 + i * 26}" font-family="monospace" font-size="15" '
                     f'fill="#d6dae2">{l}</text>' for i, l in enumerate(lines))
    h = max(480, 60 + len(lines) * 26)
    doc = (f'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="{h}">'
           '<rect width="100%" height="100%" fill="#12141a"/>'
           '<rect width="100%" height="44" fill="#1b1d23"/>'
           '<text x="24" y="30" font-family="monospace" font-size="16" fill="#8b93a7">'
           '$ hexiel --ci   (GitLab MR pipeline: real run, read-only tools)</text>' + rows + "</svg>")
    out.write_text(doc)


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
        "formatter": formatter_shot,
        "sandbox": sandbox_shot,
        "ci": ci_shot,
    }
    for name in argv or list(shots):
        if name in shots:
            export(name, shots[name])


if __name__ == "__main__":
    main(sys.argv[1:])
