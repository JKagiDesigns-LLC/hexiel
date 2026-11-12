"""Evidence screenshot: /marketplace in the terminal UI lists the built-in starter skills.
    .venv/bin/python scripts/screenshot_market.py OUT_DIR"""
import asyncio, os, subprocess, sys, tempfile
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/hexiel-market"); OUT.mkdir(parents=True, exist_ok=True)
tmp = Path(tempfile.mkdtemp(prefix="hexiel-mk-"))
os.environ.update(HEXIEL_DATA=str(tmp / "d"), HEXIEL_HOME=str(tmp / "h"), HEXIEL_NO_REPORTS="1", HEXIEL_CLOUD_PROBE="off")
from hexiel.config import ModelConfig, Settings
from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, build_tools
from hexiel.tui import HexielTUI

async def main():
    tools = build_tools(Toolchain())
    s = Session(ModelConfig(name="demo", provider="openai", model="demo", base_url="http://127.0.0.1:9/v1"), Settings(True), str(tmp), tools, "demo")
    s.bind_tool_ctx(ToolContext(cwd=str(tmp), chain=Toolchain(), tools=tools)); s.save = lambda: None
    app = HexielTUI(s, [], set())
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause(0.3)
        for cmd in ("/marketplace search", "/marketplace install commit-message"):
            app.maybe_command(cmd); await pilot.pause(0.5)
        svg = app.export_screenshot()
    (OUT / "marketplace.svg").write_text(svg)
    subprocess.run(["chromium", "--headless=new", "--disable-gpu", "--hide-scrollbars", "--window-size=1500,820", "--virtual-time-budget=3000",
                    f"--screenshot={OUT / 'marketplace.png'}", f"file://{OUT / 'marketplace.svg'}"], check=True, timeout=120, capture_output=True)
asyncio.run(main()); print("wrote", OUT / "marketplace.png")
