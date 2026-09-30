"""Generate the web-UI screenshot: seed a demo session with a realistic
conversation, serve it, and capture a chromium headless screenshot.

Run: .venv/bin/python scripts/screenshot_web.py [output-dir]"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(Path.home() / "hexiel")

from hexiel.config import load_config
from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, build_tools

PORT = 8820


def seed() -> Session:
    cfg = load_config()
    chain = Toolchain()
    tools = build_tools(chain)
    ctx = ToolContext(cwd=str(Path.home() / "hexiel"), chain=chain)
    session = Session(cfg.models["glm-flash"], cfg.settings,
                      cwd=str(Path.home() / "hexiel"), tools=tools,
                      system_prompt="demo")
    session.bind_tool_ctx(ctx)
    session.messages = [
        {"role": "user", "content": "why is the login test failing?"},
        {"role": "assistant", "content": "I'll look — starting with the test and the auth module.",
         "tool_calls": [
             {"id": "c1", "name": "test", "arguments": {"runner": "pytest", "path": "tests/test_login.py"}},
             {"id": "c2", "name": "grep", "arguments": {"pattern": "def login", "path": "src/auth.py"}},
         ]},
        {"role": "tool", "tool_call_id": "c1",
         "content": "tests/test_login.py::test_login_expired_token FAILED\n    assert 'session expired' in resp.text\n\nE   AssertionError: expected 'session expired' in body"},
        {"role": "tool", "tool_call_id": "c2", "content": "src/auth.py:112: def login(user, token):"},
        {"role": "assistant", "content": "Found it: `login()` in [auth.py:112](src/auth.py) compares the raw "
         "token instead of the decoded session, so expired sessions are accepted. The fix is a two-line change:\n\n"
         "```python\npayload = decode_session(token, verify=True)\nif payload[\"exp\"] < time.time():\n    return fail(resp, \"session expired\")\n```\n\n"
         "Applying it and re-running the login tests now."},
    ]
    return session


def main() -> None:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else Path.home() / "hexiel" / "docs" / "screenshots")
    out_dir.mkdir(parents=True, exist_ok=True)

    from hexiel.webui import make_app, serve
    session = seed()

    # serve in a background thread with real-time capture via chromium
    t = threading.Thread(
        target=serve, args=(session,), kwargs={"host": "127.0.0.1", "port": PORT}, daemon=True)
    t.start()
    time.sleep(2.0)

    png = out_dir / "web.png"
    cmd = [
        "chromium", "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--window-size=1440,900", "--screenshot=" + str(png),
        "--virtual-time-budget=12000", f"http://127.0.0.1:{PORT}/",
    ]
    subprocess.run(cmd, check=False, capture_output=True, timeout=90)
    session.stop()
    if png.exists() and png.stat().st_size > 20000:
        print(f"png: {png} ({png.stat().st_size // 1024} KB)")
    else:
        print("screenshot missing or too small:", png)


if __name__ == "__main__":
    main()