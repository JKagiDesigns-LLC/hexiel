"""Regenerate every README screenshot from an isolated demo environment
(demo config + demo project, so no real paths, hosts or keys leak into docs).

  .venv/bin/python scripts/screenshots.py [output-dir]

Needs chromium on PATH (used both for the web UI and to rasterize the TUI's SVG
captures with proper fonts)."""

import asyncio
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else REPO / "docs" / "screenshots").resolve()

# ---- isolated demo environment: must be set before hexiel is imported
DEMO = Path(tempfile.mkdtemp(prefix="hexiel-demo-"))
HOME = DEMO / "home"
PROJECT = HOME / "code" / "storefront"
os.environ.update(HOME=str(HOME), HEXIEL_HOME=str(HOME / ".config/hexiel"),
                  HEXIEL_DATA=str(HOME / ".local/share/hexiel"), HEXIEL_NO_UPDATE_CHECK="1")
(HOME / ".config/hexiel").mkdir(parents=True)
(HOME / ".config/hexiel/config.toml").write_text('''default = "local"

[settings]
auto_approve = false
compact_threshold = 0.82

[models.local]
provider = "openai"            # Ollama on a home server
base_url = "http://127.0.0.1:9/v1"
model = "qwen3.6:35b-a3b-coding"
context_window = 65536
max_output_tokens = 8192
extra_body = { reasoning_effort = "none" }

[models.sonnet]
provider = "anthropic"
model = "claude-sonnet-5-5"
api_key_env = "ANTHROPIC_API_KEY"
context_window = 200000
''')
for rel, body in {"src/app/login/login.component.ts": "export class LoginComponent {}\n",
                  "src/app/auth/auth.service.ts": "export class AuthService {}\n",
                  "backend/src/main/java/shop/AuthController.java": "class AuthController {}\n",
                  "package.json": "{}\n", "pom.xml": "<project/>\n"}.items():
    (PROJECT / rel).parent.mkdir(parents=True, exist_ok=True)
    (PROJECT / rel).write_text(body)
os.chdir(PROJECT)
sys.path.insert(0, str(REPO))

from hexiel import skills as skills_mod          # noqa: E402
from hexiel.main import build_session            # noqa: E402

CONVERSATION = [
    {"role": "user", "content": "the login page accepts expired sessions — find out why and fix it"},
    {"role": "assistant", "content": "I'll check the auth service and run the login tests first.",
     "tool_calls": [
         {"id": "c1", "name": "grep", "arguments": {"pattern": "isExpired", "path": "src/app"}},
         {"id": "c2", "name": "test", "arguments": {"path": "src/app/login"}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "src/app/auth/auth.service.ts:41:  isExpired(token) { return false; }"},
    {"role": "tool", "tool_call_id": "c2", "content": "1 failed, 23 passed\nLoginComponent › rejects an expired session\n  expected 'Session expired' to be shown"},
    {"role": "assistant", "content": "Found it — `isExpired()` in `auth.service.ts:41` is a stub that always "
     "returns `false`, so the guard never redirects. Decoding the token's `exp` claim fixes it:\n\n"
     "```ts\nisExpired(token: string): boolean {\n  const { exp } = jwtDecode<{ exp: number }>(token);\n"
     "  return exp * 1000 < Date.now();\n}\n```",
     "tool_calls": [{"id": "c3", "name": "edit", "arguments": {"path": "src/app/auth/auth.service.ts"}}]},
    {"role": "tool", "tool_call_id": "c3", "content": "edited src/app/auth/auth.service.ts (1 replacement)"},
    {"role": "assistant", "content": "Patched. Re-running the login suite to confirm."},
]


def session(conversation=True):
    s = build_session(str(PROJECT), None)
    s.messages = [dict(m) for m in CONVERSATION] if conversation else []
    return s


def svg_to_png(svg: Path, png: Path) -> None:
    head = svg.read_text()[:600]
    w, h = (float(x) for x in re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', head).groups())
    html = svg.with_suffix(".html")
    html.write_text(f"<html><body style='margin:0;background:#0c0c0c'>{svg.read_text()}</body></html>")
    chromium(f"file://{html}", png, int(w), int(h))


def chromium(url: str, png: Path, w: int = 1440, h: int = 900) -> None:
    subprocess.run(["chromium", "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    f"--window-size={w},{h}", f"--screenshot={png}", "--virtual-time-budget=8000", url],
                   capture_output=True, timeout=120, check=False)
    print("wrote", png.relative_to(OUT.parent) if OUT.parent in png.parents else png)


async def tui_shots(tmp: Path) -> None:
    from hexiel.tui import ActivityLine, HexielTUI
    sk = skills_mod.discover(str(PROJECT))

    async def capture(name, setup, size=(118, 34), conversation=True):
        app = HexielTUI(session(conversation), sk, skills_mod.command_names(sk))
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.3)
            await setup(app, pilot)
            app.save_screenshot(filename=f"{name}.svg", path=str(tmp))
        svg_to_png(tmp / f"{name}.svg", OUT / f"{name}.png")

    async def main_view(app, pilot):
        app.query_one(ActivityLine).start()
        app.query_one(ActivityLine).add_chars(2400)
        app.query_one(ActivityLine).phase("tool", "test")
        await pilot.pause(0.5)

    async def menu(app, pilot):
        await pilot.press("/", "m")
        await pilot.pause(0.3)

    async def config(app, pilot):
        app.submit_text("/config")
        await pilot.pause(0.4)
        table = app.screen.query_one("#config-table")
        table.move_cursor(row=2)            # Thinking
        await pilot.press("enter")
        await pilot.pause(0.3)

    async def nothing(app, pilot):
        await pilot.pause(0.2)

    await capture("tui-welcome", nothing, size=(100, 22), conversation=False)
    await capture("tui", main_view)
    await capture("tui-menu", menu)
    await capture("tui-config", config)


def web_shots() -> None:
    from hexiel.webui import serve
    port = 8823
    threading.Thread(target=serve, args=(session(), "127.0.0.1", port), daemon=True).start()
    time.sleep(2)
    chromium(f"http://127.0.0.1:{port}/", OUT / "web.png")
    chromium(f"http://127.0.0.1:{port}/#settings", OUT / "web-settings.png")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="hexiel-shots-"))
    asyncio.run(tui_shots(tmp))
    web_shots()
