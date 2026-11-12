"""Evidence screenshots for the 0.9.8 video tickets: what the `video` tool makes appears right in the chat.

    .venv/bin/python scripts/screenshot_098.py OUT_DIR   → OUT_DIR/web-video-chat.png, tui-video-chat.png

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

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/hexiel-shots-098")
SCRATCH = Path(tempfile.mkdtemp(prefix="hexiel-098-"))
os.environ["HEXIEL_DATA"] = str(SCRATCH / "data")
os.environ["HEXIEL_HOME"] = str(SCRATCH / "home")
os.environ["HEXIEL_NO_REPORTS"] = "1"
os.environ["HEXIEL_CLOUD_PROBE"] = "off"

from hexiel.config import ModelConfig, Settings  # noqa: E402
from hexiel.session import Session  # noqa: E402
from hexiel.tooling import Toolchain  # noqa: E402
from hexiel.tools import ToolContext, build_tools  # noqa: E402

MC = ModelConfig(name="scripted", provider="openai", model="text-only-demo", base_url="http://127.0.0.1:9/v1")
USER = ("trim the first 6 seconds of demo.mp4, caption it 'hexiel video tool', make a 480px GIF of it, "
        "pull the audio out as mp3 and show me a contact sheet")


def make_clip(d: Path) -> None:
    from hexiel import videotools as vt
    subprocess.run([vt.ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=960x540:rate=24:duration=12", "-f", "lavfi", "-i", "sine=frequency=330:duration=12",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(d / "demo.mp4")], check=True)


def scripted(project: Path) -> Session:
    calls1 = [
        {"id": "c1", "name": "video", "arguments": {"op": "trim", "path": "demo.mp4", "start": 0, "end": 6, "out": "clip.mp4"}},
        {"id": "c2", "name": "video", "arguments": {"op": "edit", "path": "clip.mp4", "out": "captioned.mp4",
         "steps": [{"do": "resize", "width": 640}, {"do": "text", "text": "hexiel video tool", "at": "top", "size": 40}]}},
    ]
    calls2 = [
        {"id": "c3", "name": "video", "arguments": {"op": "gif", "path": "captioned.mp4", "width": 320, "out": "captioned.gif"}},
        {"id": "c4", "name": "video", "arguments": {"op": "audio", "path": "clip.mp4", "mode": "extract", "format": "mp3", "out": "clip.mp3"}},
        {"id": "c5", "name": "video", "arguments": {"op": "frames", "path": "captioned.mp4", "mode": "sheet", "every": 1.5, "out": "frames"}},
    ]
    replies = [
        {"role": "assistant", "content": "Trimming and captioning with ffmpeg — it runs locally, no script and no model needed.", "tool_calls": calls1},
        {"role": "assistant", "content": "Now the GIF, the audio and a contact sheet.", "tool_calls": calls2},
        {"role": "assistant", "content": "Done: a 6-second captioned clip, a GIF, the mp3 and a contact sheet of frames. You can play them right here."},
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
        loop.run_until_complete(aioweb.TCPSite(runner, "127.0.0.1", 8792).start())
        started.set()
        loop.run_forever()
    threading.Thread(target=serve, daemon=True).start()
    started.wait(10)
    time.sleep(0.5)
    OUT.mkdir(parents=True, exist_ok=True)
    chromium("http://127.0.0.1:8792/", OUT / "web-video-chat.png", (1280, 1700))
    print([p.name for p in sorted(project.iterdir())])


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
    (OUT / "tui-video-chat.svg").write_text(svg)
    chromium(f"file://{OUT / 'tui-video-chat.svg'}", OUT / "tui-video-chat.png", (1500, 1200), 3000)


if __name__ == "__main__":
    which = sys.argv[2] if len(sys.argv) > 2 else "all"
    if which in ("all", "web"):
        proj = SCRATCH / "web-project"
        proj.mkdir()
        make_clip(proj)
        web(proj)
    if which in ("all", "tui"):
        proj = SCRATCH / "tui-project"
        proj.mkdir()
        make_clip(proj)
        asyncio.run(tui(proj))
    print("scratch:", SCRATCH)
