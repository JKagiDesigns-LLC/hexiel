"""Evidence screenshots for the 0.9.7 image tickets: what the `image` tool makes appears right in the chat.

    .venv/bin/python scripts/screenshot_097.py OUT_DIR   → OUT_DIR/web-image-*.png, tui-image-*.png

Everything shown is real: the real Session runs the real `image` tool on real files, the real web UI serves the
page and the files, a headless Chromium takes the picture. Only the model is scripted (its replies are fixed),
because what is being shown is the tool and the viewers, not a model's wording."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/hexiel-shots-097")
SCRATCH = Path(tempfile.mkdtemp(prefix="hexiel-097-"))
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
USER = ("make a 1200×300 banner that says 'Summer Sale', give photo.png a 16:9 crop with a caption and rounded "
        "corners, put both on one sheet, and show me what changed between photo.png and photo-v2.png")


def make_photos(d: Path) -> None:
    """Two 'photos' (drawn, so the demo needs no real picture): v2 has an extra object."""
    img = Image.new("RGB", (1200, 800))
    px = img.load()
    for y in range(800):
        for x in range(1200):
            t = y / 800
            px[x, y] = (int(255 - 90 * t), int(150 - 60 * t + 40 * (x / 1200)), int(90 + 140 * t))
    dr = ImageDraw.Draw(img)
    dr.ellipse((820, 90, 1000, 270), fill=(255, 236, 160))                       # sun
    dr.polygon([(0, 800), (300, 470), (560, 800)], fill=(28, 48, 74))             # hills
    dr.polygon([(380, 800), (760, 400), (1200, 800)], fill=(20, 36, 58))
    img.save(d / "photo.png")
    dr.rectangle((140, 560, 230, 650), fill=(214, 64, 69))                        # something new in v2
    dr.ellipse((560, 120, 640, 200), fill=(255, 255, 255))
    img.save(d / "photo-v2.png")


def scripted(project: Path) -> Session:
    calls1 = [
        {"id": "c1", "name": "image", "arguments": {"op": "create", "width": 1200, "height": 300, "out": "banner.png",
         "steps": [{"do": "gradient", "from": "#1e3a8a", "to": "#9333ea", "direction": "horizontal"},
                   {"do": "text", "text": "Summer Sale", "size": 120, "color": "white", "stroke": 3, "stroke_color": "#111827"}]}},
        {"id": "c2", "name": "image", "arguments": {"op": "edit", "path": "photo.png", "out": "photo-wide.png",
         "steps": [{"do": "crop", "aspect": "16:9"}, {"do": "resize", "width": 900},
                   {"do": "text", "text": "Golden hour", "at": "bottom", "size": 54, "color": "white", "background": "black"},
                   {"do": "round_corners", "radius": 40}]}},
    ]
    calls2 = [
        {"id": "c3", "name": "image", "arguments": {"op": "combine", "paths": ["banner.png", "photo-wide.png"],
         "layout": "column", "gap": 24, "background": "#f1f5f9", "out": "sheet.png"}},
        {"id": "c4", "name": "image", "arguments": {"op": "diff", "path": "photo.png", "other": "photo-v2.png", "out": "changes.png"}},
    ]
    replies = [
        {"role": "assistant", "content": "I'll make the banner and the captioned crop first — both are done locally "
         "with Pillow, so no vision model is needed.", "tool_calls": calls1},
        {"role": "assistant", "content": "Now the sheet and the comparison.", "tool_calls": calls2},
        {"role": "assistant", "content": "Done. The banner, the cropped photo with its caption, the combined sheet and "
         "the change map are in the project folder. The diff found two new objects in photo-v2.png "
         "(the red box and the white circle), marked with red outlines."},
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
    s = scripted(project)
    s.run_turn(USER)                                           # the real turn, real tool calls
    loop = asyncio.new_event_loop()
    runner = aioweb.AppRunner(webui.make_app(s))
    started = threading.Event()

    def serve():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(runner.setup())
        loop.run_until_complete(aioweb.TCPSite(runner, "127.0.0.1", 8791).start())
        started.set()
        loop.run_forever()
    threading.Thread(target=serve, daemon=True).start()
    started.wait(10)
    time.sleep(0.5)
    OUT.mkdir(parents=True, exist_ok=True)
    chromium("http://127.0.0.1:8791/", OUT / "web-image-chat.png", (1280, 1700))
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
    (OUT / "tui-image-chat.svg").write_text(svg)
    chromium(f"file://{OUT / 'tui-image-chat.svg'}", OUT / "tui-image-chat.png", (1500, 1200), 3000)


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
