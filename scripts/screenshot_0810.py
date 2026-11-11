"""Ticket evidence screenshots for the 0.8.10 / 0.9.0 batch: the budget bar that follows the provider
(TUI sidebar + web sidebar block), the knowledge passages attached to a message, and the doctor report.

    .venv/bin/python scripts/screenshot_0810.py [spend|spend_web|knowledge|doctor]   → /tmp/opencode/tickets/<name>.png
"""
import asyncio
import html
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT = Path("/tmp/opencode/tickets")
SCRATCH = Path("/tmp/opencode/ssdata-0810")
os.environ["HEXIEL_DATA"] = str(SCRATCH / "data")
os.environ["HEXIEL_KNOWLEDGE"] = str(SCRATCH / "knowledge")
os.environ["HEXIEL_NO_REPORTS"] = "1"
for env in ("OLLAMA_API_KEY", "ANTHROPIC_ADMIN_KEY", "OPENAI_ADMIN_KEY"):
    os.environ.pop(env, None)


def _terminal_svg(title: str, lines: list[str], out: Path, width: int = 1200) -> None:
    """A terminal-looking SVG for plain text output (ANSI-free), like the --ci shot in screenshot_0807."""
    rows = "".join(f'<text xml:space="preserve" x="24" y="{76 + i * 24}" font-family="JetBrains Mono, monospace" font-size="15" '
                   f'fill="{_colour(l)}">{html.escape(l)}</text>' for i, l in enumerate(lines))
    h = max(420, 100 + len(lines) * 24)
    doc = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{h}">'
           '<rect width="100%" height="100%" fill="#12141a"/>'
           '<rect width="100%" height="44" fill="#1b1d23"/>'
           f'<text x="24" y="30" font-family="monospace" font-size="16" fill="#8b93a7">{html.escape(title)}</text>'
           + rows + "</svg>")
    out.write_text(doc)


def _colour(line: str) -> str:
    s = line.strip()
    if s.startswith("- OK"):
        return "#79c98b"
    if s.startswith("- WARNING"):
        return "#d9b46a"
    if s.startswith("- ERROR"):
        return "#e08a8a"
    if s.startswith("- INFO"):
        return "#8b919d"
    if s.startswith("##") or s.startswith("hexiel ") or s.startswith("="):
        return "#7fb3a5"
    if s.startswith("- ADVICE") or s.startswith("["):
        return "#b9c0d4"
    return "#d6dae2"


def _fake_remote(usd: float, left_included: float, left_purchased: float, allowance: float,
                 period=("2026-11-02", "2026-12-02"), days: dict | None = None) -> None:
    """What spend.refresh writes after reading ollama.com — the figures of the real account on 2026-11-11."""
    from hexiel import spend
    spend._write("spend-remote.json", {"ollama": {
        "since": period[0], "at": time.time(), "usd": usd, "rates": {"glm-5.3-flash": 0.034},
        "plan_usd": allowance, "plan_name": "", "period_from": period[0], "period_until": period[1],
        "included_left": left_included, "purchased_left": left_purchased,
        "days": days or {"2026-11-05": 41.0, "2026-11-06": 44.2, "2026-11-07": 39.8, "2026-11-08": 47.1,
                         "2026-11-09": 12.56, "2026-11-10": 13.05, "2026-11-11": 5.37}}})
    spend._snap["at"] = 0.0


def _session(cwd: str):
    from hexiel.config import ModelConfig, Settings
    from hexiel.session import Session
    mc = ModelConfig(name="glm-flash", provider="openai", model="glm-5.3-flash:cloud",
                     base_url="http://127.0.0.1:11434/v1", context_window=200_000)
    s = Session(mc, Settings(spend={"ollama": {"limit": 60, "cycle_day": 23}}), cwd=cwd, tools=[], system_prompt="t")
    return s


async def spend_shot(out: Path) -> None:
    """The TUI sidebar: $378 of $380 (plan + bought credits), $1.57 left, renews Dec 2, today, pace, runway."""
    from hexiel.tui import HexielTUI
    _fake_remote(378.38, 0.0, 1.57, 300.0)
    project = SCRATCH / "shop"
    project.mkdir(parents=True, exist_ok=True)
    session = _session(str(project))
    session.messages = [
        {"role": "user", "content": "add a 'renews' date next to the budget in the sidebar"},
        {"role": "assistant", "content": "Done — the sidebar now reads the plan's own billing period from Ollama "
                                         "and shows what is left, including the credits you bought on top."}]
    app = HexielTUI(session, [], set())
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause(0.5)
        app.query_one("Sidebar").refresh_text()
        await pilot.pause(0.5)
        out.write_text(app.export_screenshot())


async def spend_topup_shot(out: Path) -> None:
    """After buying $40 of credits: the bar grows instead of overflowing, and the top-up is announced once."""
    from hexiel import spend
    from hexiel.tui import HexielTUI
    _fake_remote(378.38, 0.0, 41.57, 300.0)
    spend._write("spend-alerts.json", {})
    project = SCRATCH / "shop"
    project.mkdir(parents=True, exist_ok=True)
    session = _session(str(project))
    session.messages = [{"role": "user", "content": "keep going with the sidebar work"}]
    app = HexielTUI(session, [], set())
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause(0.5)
        for note in spend.alerts(session.settings):
            app.add_note(note)
        app.query_one("Sidebar").refresh_text()
        await pilot.pause(0.5)
        out.write_text(app.export_screenshot())


async def knowledge_shot(out: Path) -> None:
    """A Java/Cucumber project: topics detected without pinning, passages attached to the message, cost line."""
    from hexiel import knowledge
    lib = SCRATCH / "knowledge"
    for topic, name, text in (
            ("java-21", "records.md", "# Language\n\n## Records\nA record is a transparent carrier for a fixed set of "
                                      "values: `record Point(int x, int y) {}` gets a canonical constructor, accessors, "
                                      "equals, hashCode and toString. Compact constructors validate components.\n"),
            ("java-8", "records.md", "# Language\n\n## Records\nRecords do not exist in this release; write a final "
                                     "class with private fields and getters.\n"),
            ("cucumber-7", "hooks.md", "# Hooks\n\n## Before and After\nUse `@Before` and `@After` from io.cucumber.java "
                                       "to run a step before or after each scenario; order them with `@Before(order = 10)`.\n"),
            ("tomcat-10.1", "ssl.md", "# Connector\n\n## SSL\nSet `SSLEnabled=\"true\"` on the Connector in server.xml.\n")):
        (lib / topic).mkdir(parents=True, exist_ok=True)
        (lib / topic / name).write_text(text)
    knowledge.sync(say=lambda _m: None, embed=False)
    project = SCRATCH / "orders"
    (project / "src" / "test" / "resources" / "features").mkdir(parents=True, exist_ok=True)
    (project / "pom.xml").write_text("<project><properties><java.version>21</java.version></properties><dependencies>"
                                     "<dependency><groupId>io.cucumber</groupId><artifactId>cucumber-java</artifactId>"
                                     "<version>7.18.0</version></dependency></dependencies></project>")
    (project / "src" / "test" / "resources" / "features" / "checkout.feature").write_text("Feature: checkout\n")
    message = "write a @Before hook that resets the cart, and model the cart line as a record Point-style value"
    note = knowledge.auto_context(str(project), message)
    stats = knowledge.auto_stats()
    lines = [f"$ cd orders && hexiel", "",
             "# Knowledge library (from the system prompt)",
             *[l for l in knowledge.prompt_section(str(project)).splitlines() if l.startswith(("Topics", "This project"))],
             "", f"❯ {message}", "",
             *note.splitlines(),
             "", f"/knowledge → auto-attached this session: {stats['passages']} passage(s) on {stats['attached']} of "
                 f"{stats['turns']} message(s) · {stats['last_ms']:.0f} ms per message"]
    _terminal_svg("hexiel — knowledge passages attached to the message (no pin, no model call)", lines, out)


async def doctor_shot(out: Path) -> None:
    """hexiel --doctor on this machine: grouped, with advice under the non-OK lines."""
    from hexiel import doctor
    items = doctor.checks(str(Path.home() / "code" / "orders")) if (Path.home() / "code" / "orders").exists() \
        else doctor.checks(str(SCRATCH / "orders"))
    text = doctor.render(items, colour=False)
    lines = ["$ hexiel --doctor", ""] + [l.replace("c72cf89…dwc", "c72c…dwc") for l in text.splitlines()]
    _terminal_svg("hexiel --doctor — :checkhealth-style report", lines, out, width=1320)


async def web_spend_shot(out: Path) -> None:
    """The same budget block in the browser UI sidebar (served page, headless Chromium)."""
    import threading
    from aiohttp import web
    from hexiel import webui
    _fake_remote(378.38, 0.0, 1.57, 300.0)
    project = SCRATCH / "shop"
    project.mkdir(parents=True, exist_ok=True)
    session = _session(str(project))
    session.messages = [{"role": "user", "content": "show me the budget in the browser too"},
                        {"role": "assistant", "content": "The sidebar now has a 'Spend this cycle' block with the same figures."}]
    app = webui.make_app(session)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8799)
    await site.start()
    png = out.with_suffix(".png")
    try:
        await asyncio.get_running_loop().run_in_executor(None, lambda: subprocess.run(
            ["chromium", "--headless=new", "--hide-scrollbars", "--window-size=1440,900",
             f"--screenshot={png}", "--virtual-time-budget=6000", "http://127.0.0.1:8799/"],
            check=True, timeout=90, capture_output=True))
    finally:
        await runner.cleanup()
    out.write_text("")           # the PNG was written directly; export() skips the SVG conversion for an empty file


def export(name: str, coro_factory) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    svg = OUT / f"{name}.svg"
    asyncio.run(coro_factory(svg))
    png = OUT / f"{name}.png"
    if svg.stat().st_size:
        subprocess.run(["magick", "-density", "144", str(svg), "-background", "#12141a", "-resize", "1440x", str(png)],
                       check=True, timeout=120)
    print(f"{png} ({png.stat().st_size // 1024} KB)")


def main(argv: list[str]) -> None:
    shots = {"spend": spend_shot, "spend_topup": spend_topup_shot, "spend_web": web_spend_shot,
             "knowledge": knowledge_shot, "doctor": doctor_shot}
    for name in argv or list(shots):
        if name in shots:
            export(name, shots[name])


if __name__ == "__main__":
    main(sys.argv[1:])
