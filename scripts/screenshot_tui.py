"""Generate README screenshots: TUI via Textual's export_screenshot (SVG->PNG),
web UI via a live aiohttp fetch + rendered layout is handled separately.

Run: .venv/bin/python scripts/screenshot_tui.py [output-dir]"""

import asyncio
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel.config import load_config
from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, build_tools


async def capture() -> str:
    os.chdir(os.path.expanduser("~/hexiel"))
    cfg = load_config()
    chain = Toolchain()
    tools = build_tools(chain)
    ctx = ToolContext(cwd=os.getcwd().replace(os.getcwd(), "/home/josh/hexiel"), chain=chain)
    session = Session(cfg.models["glm-flash"], cfg.settings,
                      cwd=str(Path.home() / "hexiel"), tools=tools, system_prompt="demo")
    session.bind_tool_ctx(ctx)
    session.messages = [
        {"role": "user", "content": "why is the login test failing?"},
        {"role": "assistant", "content": "I'll look — starting with the test and the auth module.",
         "tool_calls": [
             {"id": "c1", "name": "test", "arguments": {"runner": "pytest", "path": "tests/test_login.py"}},
             {"id": "c2", "name": "grep", "arguments": {"pattern": "def login", "path": "src/auth.py"}},
         ]},
        {"role": "tool", "tool_call_id": "c1",
         "content": "tests/test_login.py::test_login_expired_token FAILED\n    assert 'session expired' in resp.text\n    E   AssertionError"},
        {"role": "tool", "tool_call_id": "c2", "content": "src/auth.py:112: def login(user, token):"},
        {"role": "assistant", "content": "Found it: `login()` in [auth.py:112](src/auth.py) compares the raw "
         "token instead of the decoded session, so expired sessions are accepted. The fix is a two-line change:\n\n"
         "```python\npayload = decode_session(token, verify=True)\nif payload[\"exp\"] < time.time():\n    return fail(resp, \"session expired\")\n```\n\n"
         "Applying it and re-running the login tests now."},
    ]
    from hexiel.tui import HexielTUI
    app = HexielTUI(session, [], set())
    app.session.messages = session.messages
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause(0.4)
        svg = app.export_screenshot()
    return svg


def main() -> None:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else Path.home() / "hexiel" / "docs" / "screenshots")
    out_dir.mkdir(parents=True, exist_ok=True)
    svg = asyncio.run(capture())
    svg_path = out_dir / "tui.svg"
    svg_path.write_text(svg)
    print(f"svg: {svg_path} ({len(svg) // 1024} KB)")
    png_path = out_dir / "tui.png"
    try:
        subprocess.run(["magick", "-density", "144", str(svg_path), "-background", "#12141a",
                        "-resize", "1440x", str(png_path)], check=True, timeout=120)
        print(f"png: {png_path} ({png_path.stat().st_size // 1024} KB)")
    except (subprocess.SubprocessError, FileNotFoundError):
        print("magick missing; svg kept")


if __name__ == "__main__":
    main()