"""Ticket evidence screenshots for 0.9.2 and 0.9.3.

    .venv/bin/python scripts/screenshot_092.py [name ...]   → /tmp/opencode/tickets/<name>.png

0.9.2: web_welcome, web_setup, web_health, plain_errors, budget_stop, ci
0.9.3: symbols, vision, inventory, condense, context, helpers
"""
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import screenshot_0810 as base  # noqa: E402  (sets HEXIEL_DATA etc. to a scratch dir)

SCRATCH = Path("/tmp/opencode/ssdata-092")
os.environ["HEXIEL_DATA"] = str(SCRATCH / "data")
os.environ["HEXIEL_HOME"] = str(SCRATCH / "home")
REPO = Path(__file__).resolve().parent.parent
PORT = 8798

CONFIG = """default = "glm-flash"

[models.glm-flash]
provider = "openai"
model = "glm-5.3-flash:cloud"
base_url = "http://127.0.0.1:11434/v1"
api_key_env = "OLLAMA_API_KEY"  # pragma: allowlist secret

[models.anthropic-sonnet]
provider = "anthropic"
model = "claude-sonnet-5-5"
api_key_env = "ANTHROPIC_API_KEY"  # pragma: allowlist secret

[models.qwen-local]
provider = "openai"
model = "qwen3.6:35b-a3b-coding"
base_url = "http://127.0.0.1:11434/v1"

[models.laptop-vision]
provider = "openai"
model = "gemma4:12b"
base_url = "http://127.0.0.1:11434/v1"

[tool_models]
describe_image = { models = ["laptop-vision"], timeout = 600, max_tokens = 400 }

[spend.ollama]
limit = 300
stop_at = 1.0
"""


def _config():
    import hexiel.config as config
    home = SCRATCH / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(CONFIG)
    config.CONFIG_FILE = home / "config.toml"
    os.environ["OLLAMA_API_KEY"] = "sk-ollama-demo-0123456789abcdef"  # pragma: allowlist secret
    os.environ.pop("ANTHROPIC_API_KEY", None)
    return config.load_config()


def _session(cwd: Path, model: str = "glm-flash"):
    from hexiel.session import Session
    from hexiel.tooling import Toolchain
    from hexiel.tools import ToolContext, build_tools
    cfg = _config()
    cwd.mkdir(parents=True, exist_ok=True)
    tools = build_tools(Toolchain())
    s = Session(cfg.model(model), cfg.settings, cwd=str(cwd), tools=tools, system_prompt="t")
    s.bind_tool_ctx(ToolContext(cwd=str(cwd), chain=Toolchain()))
    return s


async def _web(session, out: Path, path: str, size="1440,900", wait=6000) -> None:
    from aiohttp import web
    from hexiel import webui
    app = webui.make_app(session)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT)
    await site.start()
    png = out.with_suffix(".png")
    try:
        await asyncio.get_running_loop().run_in_executor(None, lambda: subprocess.run(
            ["chromium", "--headless=new", "--hide-scrollbars", f"--window-size={size}", f"--screenshot={png}",
             f"--virtual-time-budget={wait}", f"http://127.0.0.1:{PORT}/{path}"],
            check=True, timeout=120, capture_output=True))
    finally:
        await runner.cleanup()
    out.write_text("")


# ---------------------------------------------------------------- 0.9.2

async def web_welcome(out: Path) -> None:
    """The empty web chat: a welcome card with example prompts and links to Setup and Health."""
    await _web(_session(SCRATCH / "shop"), out, "")


async def web_setup(out: Path) -> None:
    """The web Setup page: keys per variable (masked), default model, budget with the hard-stop box."""
    base._fake_remote(212.40, 87.60, 0.0, 300.0)
    s = _session(SCRATCH / "shop")
    await _web(s, out, "#setup")


async def web_health(out: Path) -> None:
    """The web Health page: the doctor's grouped checks with the fix under each problem."""
    import hexiel.doctor as doctor
    doctor.probe_model = lambda mc, key="": "ok" if mc.name == "glm-flash" else "401 unauthorized"
    await _web(_session(SCRATCH / "shop"), out, "#health", wait=15000)


async def plain_errors(out: Path) -> None:
    """Raw provider/transport failures next to what hexiel now says (explain.explain / explain.crash)."""
    from hexiel import explain
    from hexiel.config import ModelConfig
    mc = ModelConfig(name="anthropic-sonnet", provider="anthropic", model="claude-sonnet-5-5",
                     api_key_env="ANTHROPIC_API_KEY")  # pragma: allowlist secret
    local = ModelConfig(name="qwen-local", provider="openai", model="qwen3.6", base_url="http://127.0.0.1:11434/v1")
    cases = [
        (mc, 'HTTP 401: {"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}'),
        (local, "[Errno 111] Connection refused"),
        (mc, 'HTTP 429: {"type":"error","error":{"type":"rate_limit_error","message":"Number of request tokens has exceeded your per-minute rate limit"}}'),
        (local, 'HTTP 404: {"error":"model \\"qwen3.6\\" not found, try pulling it first"}'),
        (mc, "HTTP 529: Overloaded"),
    ]
    lines = []
    for m, raw in cases:
        lines += [f"[raw]  {raw[:118]}", f"  →    {explain.explain(raw, m)}", ""]
    try:
        raise KeyError("tool_call_id")
    except KeyError as exc:
        lines += ["[crash] KeyError('tool_call_id') + traceback", f"  →    {explain.crash(exc)}"]
    wrapped = []
    for ln in lines:
        while len(ln) > 150:
            cut = ln.rfind(" ", 0, 150)
            wrapped.append(ln[:cut])
            ln = "       " + ln[cut + 1:]
        wrapped.append(ln)
    base._terminal_svg("plain-language errors: what the provider said → what hexiel shows", wrapped, out, width=1400)


async def budget_stop(out: Path) -> None:
    """[spend.ollama] stop_at = 1.0 with the cycle's $300 spent: the request is refused before it is sent, with
    the reason and the way out — in the TUI."""
    from hexiel.tui import HexielTUI
    base._fake_remote(304.12, 0.0, 0.0, 300.0)
    s = _session(SCRATCH / "shop")
    app = HexielTUI(s, [], set())
    async with app.run_test(size=(130, 34)) as pilot:
        await pilot.pause(0.5)
        app.submit_text("add pagination to the orders page")
        for _ in range(40):
            await pilot.pause(0.25)
            if not getattr(app, "busy", False):
                break
        await pilot.pause(0.5)
        app.query_one("Sidebar").refresh_text()
        await pilot.pause(0.3)
        out.write_text(app.export_screenshot())


async def ci(out: Path) -> None:
    """The pipeline definition: tests on every push, installer smoke tests on clean runners, deploy gated on both."""
    import re
    text = (REPO / ".gitlab-ci.yml").read_text().splitlines()
    lines = ["$ grep -E '^[a-z].*:|stage:|needs:|script:|allow_failure|image:|tags:' .gitlab-ci.yml", ""]
    for ln in text:
        if re.match(r"^[a-z][\w-]*:", ln) or re.search(r"\b(stage|needs|image|allow_failure|tags):", ln) or \
                "install.sh" in ln or "install.ps1" in ln or "pytest" in ln:
            lines.append(ln.rstrip()[:150])
    lines += ["", "$ .venv/bin/python -m pytest -q   (the `tests` job, run locally)",
              os.environ.get("HEXIEL_SUITE_LINE", "")]
    base._terminal_svg(".gitlab-ci.yml — test → smoke (linux / windows / macos) → deploy", lines, out, width=1400)


# ---------------------------------------------------------------- 0.9.3

def _tool_lines(s, calls) -> list[str]:
    lines = []
    for name, args in calls:
        tool = next(t for t in s.tools if t.name == name)
        res = tool.run(args, s.tool_ctx)
        shown = ", ".join(f"{k}={v!r}" for k, v in args.items())
        lines.append(f"❯ {name}({shown})")
        lines += ["  " + ln[:160] for ln in res.text.splitlines()[:14]]
        lines.append("")
    return lines


async def symbols(out: Path) -> None:
    """The symbol tools on hexiel's own source: typo-tolerant where_is, find by meaning, callers, outline, read by symbol."""
    from hexiel import symbols as sm
    s = _session(REPO)
    t = time.time()
    sm._INDEXES.clear()
    idx = sm.index_for(str(REPO))
    cold = time.time() - t
    lines = [f"index: {len(idx.files)} files, {len(idx.all())} definitions, built in {cold:.2f}s (cached on disk after)", ""]
    lines += _tool_lines(s, [("where_is", {"name": "lod_confg"}), ("find", {"query": "budget hard stop", "limit": 5}),
                             ("callers", {"name": "over_budget"}), ("outline", {"path": "hexiel/vision.py"}),
                             ("read", {"path": "hexiel/spend.py", "symbol": "over_budget"})])
    base._terminal_svg("0.9.3 — symbol index: where_is · find · callers · outline · read(symbol=)", lines, out, width=1500)


async def vision(out: Path) -> None:
    """A screenshot attached while a text-only model (qwen3-coder) is active: hexiel asks a vision model and the
    description goes into the message; with no vision model at all, tesseract reads the text."""
    import base64
    from hexiel import digest, vision as vision_mod, planner
    from hexiel.config import ModelConfig
    s = _session(SCRATCH / "shop", "qwen-local")
    s.model_config = ModelConfig(name="qwen-local", provider="openai", model="qwen3-coder:30b",
                                 base_url="http://127.0.0.1:11434/v1")
    small = SCRATCH / "budget_small.png"           # the budget-stop screenshot, scaled down: fewer vision tokens on CPU
    subprocess.run(["magick", "/tmp/opencode/tickets/budget_stop.png", "-resize", "960x", str(small)], check=True)
    data = base64.b64encode(small.read_bytes()).decode()
    s.messages = [{"role": "user", "content": "why did hexiel not answer my request?",
                   "images": [{"media_type": "image/png", "data": data, "approx_tokens": 1500}]}]
    s.__dict__["_turn_request"] = "why did hexiel not answer my request?"
    notes = []
    s.on_event = lambda e, *p, **k: notes.append(p[0]) if e == "note" and p else None
    real = True
    try:
        vision_mod.describe_history(s)
    except Exception:  # noqa: BLE001
        real = False
    lines = [f"active model: qwen-local (qwen3-coder:30b) — sees images: {vision_mod.sees_images(s.model_config)}",
             f"[tool_models] describe_image roster: {digest.rules(s.settings).get('describe_image', {}).get('models') or ['gemma4', 'anthropic-sonnet']}",
             ""] + [f"note: {n}" for n in notes] + ["", "message the model receives:"]
    import textwrap
    for ln in s.messages[0]["content"].splitlines()[:30]:
        lines += ["  " + w for w in (textwrap.wrap(ln, 140) or [""])]
    base._terminal_svg("0.9.3 — vision for a text-only model: the image arrives as a description", lines, out, width=1400)


async def inventory(out: Path) -> None:
    from hexiel import inventory as inv
    s = _session(SCRATCH / "shop")
    data = inv.scan(force=True)
    lines = [f"PATH scanned: {data['all_count']} programs, {len(data['programs'])} catalogued, "
             f"{len(data['packages'])} Python packages · {inv.system_line()}", ""]
    for req in ("convert screen.mov to a gif", "read the text in this screenshot with OCR", "query sales.csv by region"):
        lines += [f"request: {req!r}", *("  " + ln for ln in inv.relevant(req).splitlines()), ""]
    lines += _tool_lines(s, [("have", {"names": "ffmpeg,tesseract,jq,PIL,cv2"})])
    absent = s._absent_tools()
    lines.append(f"tool definitions left out (program missing): {', '.join(sorted(absent)) or 'none'}")
    base._terminal_svg("0.9.3 — capability inventory: the relevant slice per request, `have` with install lines", lines, out, width=1400)


async def condense(out: Path) -> None:
    from hexiel import condense as cd
    noisy = "\n".join(["Collecting packages…"] + [f"Downloading chunk {i} of 3000 (0x{i:08x})" for i in range(3000)]
                      + ["ERROR: Could not find a version that satisfies the requirement torch==9.9",
                         "ERROR: No matching distribution found for torch==9.9"])
    res = cd.condense(noisy)
    status = subprocess.run(["git", "-C", str(REPO), "status"], capture_output=True, text=True).stdout
    dig = cd.structure("git status", status, min_chars=0) or ""
    lines = [f"pip install log: {len(noisy):,} chars → {len(res):,} chars", ""]
    lines += ["  " + ln for ln in res.splitlines()]
    lines += ["", f"git status: {len(status):,} chars → {len(dig):,} chars", ""] + ["  " + ln[:150] for ln in dig.splitlines()]
    base._terminal_svg("0.9.3 — no-model output condensing (repeats collapsed, errors kept, digests)", lines, out, width=1400)


async def context(out: Path) -> None:
    from hexiel import brief
    from hexiel.session import Session
    s = _session(REPO)
    first = {"role": "user", "content": "fix the flaky steering test"}
    s.messages.append(first)
    t = time.time()
    s._context_note(first, "fix the flaky steering test")
    ms = (time.time() - t) * 1000
    lines = [f"first message, attached locally in {ms:.0f} ms (no model call):", ""]
    lines += ["  " + ln[:150] for ln in first["content"].splitlines()]
    # the stamped cache + diff re-read
    f = SCRATCH / "shop" / "orders.py"
    f.write_text("\n".join(f"def handler_{i}(req):\n    return {i}\n" for i in range(60)))
    s2 = _session(SCRATCH / "shop")
    s2.on_event = lambda *a, **k: None

    def call(n, **args):
        c = {"id": f"c{n}", "name": "read", "arguments": args}
        s2.messages.append({"role": "assistant", "content": "", "tool_calls": [c]})
        s2._execute_tool(c)
        return s2.messages[-1]["content"]
    a = call(1, path="orders.py")
    b = call(2, path="orders.py")
    time.sleep(0.01)
    f.write_text(f.read_text().replace("return 30\n", "return 30 * 2\n"))
    c = call(3, path="orders.py")
    lines += ["", f"read orders.py: {len(a):,} chars", f"read again, unchanged: {b}",
              f"read after an edit: {len(c):,} chars —"] + ["  " + ln for ln in c.splitlines()[:12]]
    base._terminal_svg("0.9.3 — first-message project brief · stamped tool cache · diff-aware re-reads", lines, out, width=1400)


async def helpers(out: Path) -> None:
    s = _session(SCRATCH / "shop")
    csv = SCRATCH / "shop" / "sales.csv"
    csv.write_text("region,amount,rep\nnorth,1200,ana\nsouth,830,bo\nnorth,410,\nwest,2210,cy\n")
    lines = _tool_lines(s, [("util", {"op": "date", "expr": "2026-11-11 + 45 days"}),
                            ("util", {"op": "date", "expr": "2026-01-01 to 2026-11-11"}),
                            ("util", {"op": "units", "expr": "3.5 GiB to MB"}),
                            ("util", {"op": "semver", "expr": "sort 1.10.0 1.2.0 1.2.0-rc1"}),
                            ("util", {"op": "semver", "expr": "1.5.0 satisfies ^1.2"}),
                            ("util", {"op": "csv", "path": "sales.csv"}),
                            ("util", {"op": "json", "expr": "items[*].sku", "text": '{"items":[{"sku":"A1"},{"sku":"B7"}]}'}),
                            ("util", {"op": "calc", "expr": "1200 * 1.0825 ** 3"})])
    base._terminal_svg("0.9.3 — util: exact date math, units, semver, CSV stats, JSON paths, arithmetic", lines, out, width=1400)


SHOTS = {"web_welcome": web_welcome, "web_setup": web_setup, "web_health": web_health, "plain_errors": plain_errors,
         "budget_stop": budget_stop, "ci": ci, "symbols": symbols, "vision": vision, "inventory": inventory,
         "condense": condense, "context": context, "helpers": helpers}

if __name__ == "__main__":
    for name in sys.argv[1:] or list(SHOTS):
        base.export(name, SHOTS[name])
