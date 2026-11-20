"""Ticket evidence for 0.9.15/0.9.16: hexiel uses the existing todo.md in place, and the upload endpoint the phone
app's attach button talks to accepts any file.

    .venv/bin/python scripts/screenshot_0916.py   → /tmp/opencode/tickets/todo-md-in-place.png, phone-any-file-upload.png
"""
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["HEXIEL_NO_REPORTS"] = "1"
import hexiel  # noqa: E402
from hexiel import todo  # noqa: E402
from screenshot_0810 import OUT, _terminal_svg  # noqa: E402


def _png(name: str, title: str, lines: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    svg = OUT / f"{name}.svg"
    _terminal_svg(title, lines, svg)
    png = OUT / f"{name}.png"
    subprocess.run(["magick", "-density", "144", str(svg), "-background", "#12141a", "-resize", "1440x", str(png)], check=True)
    print(png)


def todo_shot() -> None:
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "TODO.md").write_text("# Roadmap\n\nProse the team wrote stays.\n\n## Now\n- [ ] ship the invoice export\n- [x] fix login\n\n## Notes\nkeep me\n")
        before = (Path(d) / "TODO.md").read_text().splitlines()
        todo.action_update({"step": "invoice", "state": "~"}, d)
        todo.action_add({"step": "write the release notes", "after": "invoice"}, d)
        todo.action_left_off({"note": "export half done"}, d)
        after = (Path(d) / "TODO.md").read_text().splitlines()
        files = sorted(p.name for p in Path(d).iterdir())
    lines = [f"hexiel {hexiel.__version__} — opened in a folder that already has TODO.md", "", "BEFORE  (TODO.md)", *["  " + l for l in before],
             "", "hexiel's todo tool: update step -> in progress; add a step; set 'where we left off'", "",
             "AFTER  (the same TODO.md, edited in place)", *["  " + l for l in after], "", f"files in the folder: {', '.join(files)}   (no second todo/plan file, no .hexiel/todo.md)"]
    _png("todo-md-in-place", "hexiel — todo.md in the folder hexiel was opened in", lines)


def upload_shot() -> None:
    from aiohttp.test_utils import TestClient, TestServer
    from hexiel.webui import make_app
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tests.test_remote_control import make_session
    with tempfile.TemporaryDirectory() as d:
        s = make_session(Path(d))
        rows = []

        async def go():
            async with TestClient(TestServer(make_app(s))) as client:
                for name, mime, size in (("Budget Q3.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", 48_213),
                                         ("meeting notes.docx", "application/octet-stream", 20_480), ("parser.py", "text/x-python", 3_120)):
                    r = await client.post("/api/paste-image", data=b"x" * size, headers={"content-type": mime, "x-filename": name})
                    body = await r.json()
                    rows.append(f"  POST /api/paste-image  {name:<22} {size:>7} bytes  ->  {r.status}  saved as {body.get('name')}")
        asyncio.run(go())
        saved = sorted(p.name for p in (Path(d) / ".hexiel" / "pastes").iterdir())
    lines = [f"hexiel {hexiel.__version__} — the endpoint the phone app's attach button uploads to (through the relay tunnel)", "",
             "Before 0.9.15 only png/jpeg/webp/gif/pdf were accepted; anything else was a 400.", "", *rows, "",
             "Saved in the project (never executed); the message then carries:", "  (attached file: <path>)   — the agent reads it with its file tools",
             ]
    _png("phone-any-file-upload", "hexiel — phone attachments: any file type", lines)


todo_shot()
upload_shot()
