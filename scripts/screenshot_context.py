"""Ticket evidence screenshots for the 0.8.5 speed work: /context metering output."""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, "scripts")

os.environ.setdefault("HEXIEL_DATA", "/tmp/opencode/ssdata")
os.environ.setdefault("HEXIEL_HOME", "/tmp/opencode/sshome")
os.environ["HEXIEL_NO_REPORTS"] = "1"

from hexiel.config import ModelConfig, Settings
from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, build_tools


async def capture(out: Path) -> str:
    MC = ModelConfig(name="gmktec", provider="openai", model="qwen3.6:35b-a3b-coding",
                     base_url="http://127.0.0.1:11434/v1", context_window=200_000)
    session = Session(MC, Settings(), cwd=str(Path.home() / "hexiel"),
                      tools=build_tools(Toolchain()), system_prompt="t")
    session.bind_tool_ctx(ToolContext(cwd=str(Path.home() / "hexiel"), chain=Toolchain()))
    session.messages = [
        {"role": "user", "content": "why is the login test failing?"},
        {"role": "assistant", "content": "I'll look — starting with the test and the auth module.",
         "tool_calls": [
             {"id": "c1", "name": "test", "arguments": {"runner": "pytest", "path": "tests/test_login.py"}},
             {"id": "c2", "name": "grep", "arguments": {"pattern": "def login", "path": "src/auth.py"}},
         ]},
        {"role": "tool", "tool_call_id": "c1",
         "content": "tests/test_login.py::test_login_expired_token FAILED\n" + "assertion detail\n" * 900},
        {"role": "tool", "tool_call_id": "c2", "content": "src/auth.py:112: def login(user, token):\n" + "ctx\n" * 900},
        {"role": "assistant", "content": "Found it: login() in auth.py:112 compares the raw token "
         "instead of the decoded session. Fixing it now."},
    ]
    from hexiel.tui import HexielTUI
    app = HexielTUI(session, [], set())
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause(0.3)
        app.add_note("/context")
        app.cmd_context("")
        await pilot.pause(0.6)
        svg = app.export_screenshot()
    out.write_text(svg)
    return svg


def main() -> None:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/opencode/tickets")
    out_dir.mkdir(parents=True, exist_ok=True)
    svg_path = out_dir / "context.svg"
    asyncio.run(capture(svg_path))
    png = out_dir / "context.png"
    subprocess.run(["magick", "-density", "144", str(svg_path), "-background", "#12141a",
                    "-resize", "1440x", str(png)], check=True, timeout=120)
    print(f"{png} ({png.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
