"""Evidence screenshots for the highlighting ticket: files and JSON the tools return are coloured with tree-sitter in the chat.

    .venv/bin/python scripts/screenshot_hl.py OUT_DIR   → OUT_DIR/web-hl-*.png, tui-hl-*.png

Everything shown is real: the real Session runs the real `image` tool on real files, the real web UI serves the
page and the files, a headless Chromium takes the picture. Only the model is scripted (its replies are fixed),
because what is being shown is the tool and the viewers, not a model's wording."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/hexiel-shots-hl")
SCRATCH = Path(tempfile.mkdtemp(prefix="hexiel-hl-"))
os.environ["HEXIEL_DATA"] = str(SCRATCH / "data")
os.environ["HEXIEL_HOME"] = str(SCRATCH / "home")
os.environ["HEXIEL_NO_REPORTS"] = "1"
os.environ["HEXIEL_CLOUD_PROBE"] = "off"

from PIL import Image, ImageDraw  # noqa: E402

from hexiel.config import ModelConfig, Settings  # noqa: E402
from hexiel.session import Session  # noqa: E402
from hexiel.tooling import Toolchain  # noqa: E402
from hexiel.tools import ToolContext, build_tools  # noqa: E402

MC = ModelConfig(name="scripted", provider="openai", model="text-only-demo", base_url="http://127.0.0.1:9/v1")
USER = "read app.py and config.json"
_UNUSED = ("make a 1200×300 banner that says 'Summer Sale', give photo.png a 16:9 crop with a caption and rounded "
        "corners, put both on one sheet, and show me what changed between photo.png and photo-v2.png")


def make_photos(d: Path) -> None:
    (d / "app.py").write_text('''"""Tiny web app."""
import json


class Greeter:
    def __init__(self, name: str = "world") -> None:
        self.name = name  # who to greet

    def hello(self, times: int = 2) -> str:
        return ", ".join(f"hello {self.name}" for _ in range(times))


if __name__ == "__main__":
    print(Greeter().hello(3), json.dumps({"ok": True}))
''')
    (d / "config.json").write_text(json.dumps({"name": "demo", "version": "1.2.0", "debug": False,
                                              "servers": [{"host": "a.example", "port": 8080}, {"host": "b.example", "port": None}]}, indent=2))


def scripted(project: Path) -> Session:
    calls1 = [{"id": "c1", "name": "read", "arguments": {"path": "app.py"}},
              {"id": "c2", "name": "read", "arguments": {"path": "config.json"}}]
    replies = [
        {"role": "assistant", "content": "Let me read the app and its config.", "tool_calls": calls1},
        {"role": "assistant", "content": "Both look fine: a small Greeter class and a config with two servers."},
    ]
    script = iter(replies)

    def stream_once(self, provider, defs):
        reply = next(script)
        if reply.get("content"):
            self.on_event("text", reply["content"])
        return reply
    Session._stream_once = stream_once
    import hexiel.llm as llm
    llm.make_provider = lambda *a, **k: object()
    tools = build_tools(Toolchain())
    s = Session(MC, Settings(True), str(project), tools, "demo")
    s.bind_tool_ctx(ToolContext(cwd=str(project), chain=Toolchain(), tools=tools))
    s.save = lambda: None
    return s


def chromium(url: str, png: Path, size=(1280, 1500), budget=8000) -> None:
    subprocess.run(["chromium", "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size={size[0]},{size[1]}",
                    f"--virtual-time-budget={budget}", f"--screenshot={png}", url], check=True, timeout=120, capture_output=True)
    print("wrote", png)


def web(project: Path) -> None:
    from aiohttp import web as aioweb
    from hexiel import webui
    import shutil
    copy = SCRATCH / "web"
    shutil.copytree(webui.WEB_DIR, copy)
    with open(copy / "style.css", "a") as fh:
        fh.write(".tool .preview { display: block !important; max-height: none !important; }")   # show results open
    webui.WEB_DIR = copy
    s = scripted(project)
    s.run_turn(USER)                                           # the real turn, real tool calls
    loop = asyncio.new_event_loop()
    runner = aioweb.AppRunner(webui.make_app(s))
    started = threading.Event()

    def serve():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(runner.setup())
        loop.run_until_complete(aioweb.TCPSite(runner, "127.0.0.1", 8792).start())
        started.set()
        loop.run_forever()
    threading.Thread(target=serve, daemon=True).start()
    started.wait(10)
    time.sleep(0.5)
    OUT.mkdir(parents=True, exist_ok=True)
    chromium("http://127.0.0.1:8792/", OUT / "web-hl-chat.png", (1280, 1700))
    for p in ("banner.png", "sheet.png", "changes.png"):
        print(p, Image.open(project / p).size)


async def tui(project: Path) -> None:
    from hexiel.tui import HexielTUI
    s = scripted(project)
    app = HexielTUI(s, [], set())
    async with app.run_test(size=(150, 60)) as pilot:
        await pilot.pause(0.3)
        app._start_turn(USER)
        for _ in range(100):
            await pilot.pause(0.3)
            if not (app._thread and app._thread.is_alive()):
                break
        await pilot.pause(1.0)
        svg = app.export_screenshot()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tui-hl-chat.svg").write_text(svg)
    chromium(f"file://{OUT / 'tui-hl-chat.svg'}", OUT / "tui-hl-chat.png", (1500, 1200), 3000)


if __name__ == "__main__":
    which = sys.argv[2] if len(sys.argv) > 2 else "all"
    if which in ("all", "web"):
        proj = SCRATCH / "web-project"
        proj.mkdir()
        make_photos(proj)
        web(proj)
    if which in ("all", "tui"):
        proj = SCRATCH / "tui-project"
        proj.mkdir()
        make_photos(proj)
        asyncio.run(tui(proj))
    print("scratch:", SCRATCH)
