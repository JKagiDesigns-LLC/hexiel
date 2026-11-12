"""Evidence screenshots for the 0.9.4 tickets (tree-sitter, find by meaning, vision extras, ACP, teams, marketplace).

    .venv/bin/python scripts/screenshot_094.py [name ...]   → /tmp/opencode/tickets/<name>.png

Every shot is real output from the code in this repo. ACP and teams are driven by a scripted model (the shot says
so): the protocol and the message passing are what is being shown, not a model's wording."""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import screenshot_0810 as base  # noqa: E402  (HEXIEL_DATA → a scratch dir, reports off)

OUT = base.OUT
SCRATCH = Path("/tmp/opencode/ssdata-094")
os.environ["HEXIEL_DATA"] = str(SCRATCH / "data")
os.environ["HEXIEL_HOME"] = str(SCRATCH / "home")


def shot(name: str, title: str, lines: list[str], width: int = 1400) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    svg = OUT / f"{name}.svg"
    base._terminal_svg(title, lines, svg, width=width)
    h = max(420, 100 + len(lines) * 24)
    png = OUT / f"{name}.png"
    subprocess.run(["chromium", "--headless=new", "--hide-scrollbars", f"--window-size={width},{h}",
                    f"--screenshot={png}", f"file://{svg}"], check=True, timeout=120, capture_output=True)
    print("wrote", png)
    return png


def wrap(text: str, width: int = 150) -> list[str]:
    out = []
    for line in text.splitlines():
        while len(line) > width:
            out.append(line[:width])
            line = "    " + line[width:]
        out.append(line)
    return out


# ---------------------------------------------------------------- tree-sitter

TS_SRC = '''interface Props { items: string[] }

export function Sidebar({ items }: Props) {
  const style = { color: "red" };   // a brace that is not a block
  const label = "}";                  // a brace inside a string
  return items.map(i => render(i));
}

export class Store<T> {
  private data: Map<string, T> = new Map();
  get(key: string): T | undefined {
    return this.data.get(key);
  }
}
'''


def treesitter() -> None:
    from hexiel import symbols as sm
    d = Path(tempfile.mkdtemp())
    (d / ".git").mkdir()
    (d / "sidebar.ts").write_text(TS_SRC)
    lines = ["$ cat -n sidebar.ts"] + [f"{i:>4}  {l}" for i, l in enumerate(TS_SRC.splitlines(), 1)] + [""]
    for engine in ("regex", "tree-sitter"):
        sm.ENGINE = engine
        sm._INDEXES.clear()
        idx = sm.index_for(str(d))
        lines.append(f"## outline with ENGINE = {engine!r}"
                     + ("   (pip install 'hexiel[code]')" if engine == "tree-sitter" else "   (no extra installed)"))
        for s in sorted(idx.all(), key=lambda s: s.line):
            lines.append(f"   {s.kind:<9} {s.qualname:<12} lines {s.line}-{s.end}")
        lines.append("")
    lines.append(f"tree-sitter available: {sm.treesitter_available()}  ·  grammars from tree-sitter-language-pack")
    shot("treesitter", "hexiel 0.9.4 — symbol ranges: regex outlines vs tree-sitter", lines)


# ---------------------------------------------------------------- find by meaning

def semantic() -> None:
    from hexiel import symbols as sm
    os.environ.pop("HEXIEL_EMBED", None)
    idx = sm.index_for(str(REPO))
    ep = sm.embedder()
    lines = [f"embedding endpoint: {ep}", f"definitions indexed: {len(idx.all())}"]
    t = time.time()
    sm.embed_missing(idx, ep)
    lines.append(f"vectors ready: {len(idx.vectors)} (filled/loaded in {time.time() - t:.1f}s, cached beside the index)")
    lines.append("")
    for q in ("how much money has been used this month", "turn a picture into words for a blind model"):
        for semantic_on in (False, True):
            t = time.time()
            hits = sm.find(idx, q, limit=5, semantic=semantic_on)
            lines.append(f"$ find {q!r}  — {'keywords + meaning' if semantic_on else 'keywords only'}"
                         f"  ({(time.time() - t) * 1000:.0f} ms)")
            for score, where, what in hits[:5]:
                lines.append(f"   {score:6.2f}  {where:<34} {what[:80]}")
            if not hits:
                lines.append("   (nothing)")
            lines.append("")
    shot("semantic_find", "hexiel 0.9.4 — find by meaning (nomic-embed-text via local Ollama)", lines)


# ---------------------------------------------------------------- vision extras

def vision_extras() -> None:
    from hexiel import vision
    d = Path(tempfile.mkdtemp())
    qr = d / "qr.png"
    subprocess.run(["qrencode", "-o", str(qr), "-s", "6", "https://hexiel.dev/docs/install"], check=True)
    card_img = d / "card.png"
    subprocess.run(["magick", "-size", "900x500", "xc:#f4f1ea", "-fill", "#1f4e79", "-draw", "rectangle 0,0 900,90",
                    "-fill", "white", "-pointsize", "40", "-annotate", "+30+60", "Quarterly report",
                    str(qr), "-geometry", "+640+200", "-composite", str(card_img)], check=True)
    lines = ["## image_card(card.png) — no vision model, no tesseract: Pillow + zbar only"]
    lines += ["   " + l for l in wrap(vision.image_card(card_img.read_bytes()), 140)]
    lines.append("")
    scan = d / "scan.pdf"                             # a page that is only a picture: no text layer
    subprocess.run(["magick", "-size", "1240x600", "xc:white", "-fill", "black", "-pointsize", "48",
                    "-annotate", "+80+150", "INVOICE 2026-117", str(qr), "-geometry", "+900+300", "-composite",
                    str(scan)], check=True)
    lines.append("## read scan.pdf — page 1 has no text layer, so it is rendered (pdftoppm) and looked at")
    t = time.time()
    for page, how, text in vision.pdf_text(scan):
        lines.append(f"   page {page} via {how}  ({time.time() - t:.1f}s)")
        lines += ["      " + l for l in wrap(text, 130)[:10]]
    lines.append("")
    lines.append("## render README.md — headless browser screenshot for the model to look at")
    t = time.time()
    png = vision.render("README.md", str(REPO), width=1100, height=800)
    (OUT / "render_readme.png").write_bytes(png)
    lines.append(f"   browser: {vision.find_browser()}  ·  {len(png):,} bytes PNG in {time.time() - t:.1f}s"
                 "  →  render_readme.png")
    lines.append("   vision models get the image; text-only models get the description / card instead")
    shot("vision_extras", "hexiel 0.9.4 — image card, scanned PDFs, render-and-look", lines)


# ---------------------------------------------------------------- ACP

def acp() -> None:
    import pytest  # noqa: F401 - monkeypatch below
    from _pytest.monkeypatch import MonkeyPatch
    sys.path.insert(0, str(REPO / "tests"))
    import test_acp as t
    mp = MonkeyPatch()
    d = Path(tempfile.mkdtemp())
    (d / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    from hexiel.tools import ToolDef, text_result

    def edit(args, ctx):
        p = d / args["path"]
        p.write_text(p.read_text().replace(args["old_string"], args["new_string"]))
        return text_result(f"edited {args['path']}")
    tools = [ToolDef("edit", "edit", {"type": "object", "properties": {}}, run=edit)]
    replies = [{"role": "assistant", "content": "The function subtracts. Fixing it.", "tool_calls": [
        {"id": "c1", "name": "edit", "arguments": {"path": "calc.py", "old_string": "a - b", "new_string": "a + b"}}]},
        {"role": "assistant", "content": "Changed `a - b` to `a + b` in add()."}]
    ed, _, _ = t.make(d, mp, replies, tools=tools)
    log = []
    orig_send, orig_read = ed._send, None

    def send(msg):
        log.append(("editor → hexiel", msg))
        orig_send(msg)
    ed._send = send
    seen = len(ed.updates)
    ed.call("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": True}}})
    sid = ed.call("session/new", {"cwd": str(d), "mcpServers": []})["result"]["sessionId"]
    res = ed.call("session/prompt", {"sessionId": sid, "prompt": [
        {"type": "text", "text": "the add function is wrong, fix it"},
        {"type": "resource_link", "uri": f"file://{d}/calc.py", "name": "calc.py"}]})
    mp.undo()
    lines = ["hexiel --acp  (driven over real pipes exactly as Zed/JetBrains do; scripted model)", ""]
    for who, msg in log:
        if msg.get("method"):
            lines.append(f"→ {msg['method']}  {json.dumps(msg.get('params'))[:120]}")
    lines.append("")
    lines.append("← session/update stream:")
    for u in ed.updates[seen:]:
        k = u["sessionUpdate"]
        if k == "agent_message_chunk":
            lines.append(f"   agent_message_chunk  {u['content']['text']!r}")
        elif k == "tool_call":
            lines.append(f"   tool_call            id={u['toolCallId']} kind={u['kind']} title={u['title']!r} {u['status']}")
        elif k == "tool_call_update":
            diff = next((c for c in u["content"] if c["type"] == "diff"), None)
            lines.append(f"   tool_call_update     id={u['toolCallId']} {u['status']}"
                         + (f"  diff {diff['oldText']!r} → {diff['newText']!r}" if diff else ""))
        else:
            lines.append(f"   {k}")
    lines.append("")
    for perm in ed.permissions:
        lines.append(f"← session/request_permission  toolCallId={perm['toolCall']['toolCallId']}  options="
                     + ", ".join(o["kind"] for o in perm["options"]) + "   → editor chose allow_once")
    lines.append(f"← prompt result: {json.dumps(res['result'])}")
    lines.append("")
    lines.append(f"calc.py now: {(d / 'calc.py').read_text().strip().splitlines()[-1].strip()}")
    lines.append('Zed settings.json:  "agent_servers": { "hexiel": { "command": "hexiel", "args": ["--acp"] } }')
    shot("acp", "hexiel 0.9.4 — Agent Client Protocol server", lines)


# ---------------------------------------------------------------- teams

def teams_shot() -> None:
    from _pytest.monkeypatch import MonkeyPatch
    sys.path.insert(0, str(REPO / "tests"))
    import test_teams as t
    from hexiel import teams
    mp = MonkeyPatch()

    def reviewer_final(helper):
        got = next(m["content"] for m in helper.messages if m["role"] == "user"
                   and "[team message from backend]" in str(m["content"]))
        return t.say("Reviewed: pagination param agreed (" + got.split("] ", 1)[1] + "); add a max page size.")
    scripts = {
        "backend": [lambda h: t.call("read"),
                    lambda h: t.call("team_msg", to="reviewer", text="API shape: GET /items?page=N returns 50 items"),
                    lambda h: t.say("Implemented GET /items with page=N (50 per page).")],
        "reviewer": [lambda h: t.call("team_msg", to="backend", text="what is the pagination parameter?", wait=20),
                     reviewer_final],
        "docs": [lambda h: t.call("team_msg", wait=20),
                 lambda h: t.say("Documented /items pagination in API.md.")],
    }
    t.scripted(mp, scripts)
    lead = t.lead_session(Path(tempfile.mkdtemp()))
    out = teams.run_team(lead, {"goal": "ship the paginated items API", "members": [
        {"name": "backend", "role": "build the API", "prompt": "Build it."},
        {"name": "reviewer", "role": "review the API design", "prompt": "Review it."},
        {"name": "docs", "role": "document it", "prompt": "Document it."}]})
    mp.undo()
    lines = ["the lead called:  team(goal='ship the paginated items API', members=[backend, reviewer, docs])"
             "   (scripted model)", "", "live notes the lead saw:"]
    lines += [f"   {n}" for n in lead.notes]
    lines += ["", "/team"]
    lines += ["   " + l for l in lead.team_board.render().splitlines()]
    lines += ["", "result returned to the lead:"]
    lines += ["   " + l for l in out.text.split("\n\n", 1)[-1].splitlines() if l.strip()]
    shot("teams", "hexiel 0.9.4 — agent teams with peer messaging", lines)


# ---------------------------------------------------------------- marketplace

def marketplace_shot() -> None:
    import shutil
    sys.path.insert(0, str(REPO / "tests"))
    import test_marketplace as t
    home = SCRATCH / "market"
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True)
    os.environ["HEXIEL_DATA"] = str(home / "data")
    os.environ["HEXIEL_HOME"] = str(home / "config")
    cat = t.catalogue_dir(home)
    cc = home / "cc-market"
    plug = cc / "plugins" / "reviewer"
    (cc / ".claude-plugin").mkdir(parents=True)
    (plug / "skills" / "security-review").mkdir(parents=True)
    (plug / "skills" / "security-review" / "SKILL.md").write_text(
        "---\nname: security-review\ndescription: Review a diff for security bugs\n---\nLook for injection.\n")
    (cc / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"name": "cc-tools", "plugins": [
        {"name": "reviewer", "source": "./plugins/reviewer", "description": "Code review helpers (Claude Code plugin)",
         "version": "0.3.0"}]}))
    lines = []

    def run(args: str):
        r = subprocess.run([str(REPO / ".venv/bin/hexiel"), "marketplace", *args.split()], capture_output=True,
                           text=True, env=os.environ)
        lines.append(f"$ hexiel marketplace {args.replace(str(home), '~/demo')}")
        lines.extend("  " + l for l in wrap((r.stdout + r.stderr).rstrip().replace(str(home), "~/demo"), 140))
        lines.append("")
    run(f"add {cat}")
    run(f"add {cc}")
    run("search")
    run("install pdf-tables")
    run("install reviewer")
    (cat / "shout.py").write_text((cat / "shout.py").read_text() + "import os\n")   # tampered after pinning
    run("install shout")
    run("installed")
    shot("marketplace", "hexiel 0.9.4 — skills & plugins marketplace", lines)


SHOTS = {"treesitter": treesitter, "semantic": semantic, "vision": vision_extras, "acp": acp, "teams": teams_shot,
         "marketplace": marketplace_shot}

if __name__ == "__main__":
    for name in sys.argv[1:] or list(SHOTS):
        SHOTS[name]()
