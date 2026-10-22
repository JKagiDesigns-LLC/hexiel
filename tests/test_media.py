"""Seeing what you attach: images and PDFs in the terminal chat and the web UI."""

import asyncio
import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import media

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def make_pdf(path: Path, pages: int = 2) -> Path:
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    with open(path, "wb") as f:
        w.write(f)
    return path


def test_describe_an_image_and_a_pdf(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(PNG)
    pdf = make_pdf(tmp_path / "spec.pdf", 3)
    a, b = media.describe(img), media.describe(pdf)
    assert (a["kind"], a["w"], a["h"]) == ("image", 1, 1)
    assert (b["kind"], b["pages"]) == ("pdf", 3)
    assert "spec.pdf · 3 pages" in media.label(b) and "shot.png · 1×1" in media.label(a)


def test_attachments_found_in_a_message(tmp_path):
    (tmp_path / "shot.png").write_bytes(PNG)
    make_pdf(tmp_path / "my doc.pdf")
    found = media.attachments_in(f'look at @shot.png and "{tmp_path}/my doc.pdf" and missing.png, thanks', str(tmp_path))
    assert [f["name"] for f in found] == ["shot.png", "my doc.pdf"]


def test_only_registered_files_are_served(tmp_path):
    secret = tmp_path / "notes.txt"
    secret.write_text("x")
    assert media.register(secret) is None                     # not an image or a pdf
    img = tmp_path / "a.png"
    img.write_bytes(PNG)
    media_id = media.register(img)
    assert media.resolve(media_id) == img.resolve()
    assert media.resolve("../../etc/passwd") is None and media.resolve("deadbeef") is None


def test_a_session_remembers_what_was_attached(tmp_path, monkeypatch):
    from hexiel.session import Session
    from tests.test_remote_control import make_session
    s = make_session(tmp_path)
    s.save = lambda: None
    monkeypatch.setattr(Session, "_stream_once", lambda self, provider, defs: {"role": "assistant", "content": "a pixel"})
    monkeypatch.setattr("hexiel.llm.make_provider", lambda *a, **k: object())
    (tmp_path / "shot.png").write_bytes(PNG)
    s.run_turn("what is in @shot.png")
    user = next(m for m in s.messages if m["role"] == "user")
    assert user["attachments"][0]["name"] == "shot.png" and user["images"]


def test_the_web_ui_shows_attachments_in_history_and_serves_them(tmp_path):
    aiohttp = pytest.importorskip("aiohttp")
    from aiohttp.test_utils import TestClient, TestServer
    from tests.test_remote_control import make_session
    from hexiel.webui import make_app
    s = make_session(tmp_path)
    img = tmp_path / "shot.png"
    img.write_bytes(PNG)
    pdf = make_pdf(tmp_path / "spec.pdf")
    s.messages = [{"role": "user", "content": f"see {img} and {pdf}",
                   "attachments": [{"path": str(img)}, {"path": str(pdf)}]}]

    async def go():
        async with TestClient(TestServer(make_app(s))) as client:
            ws = await client.ws_connect("/ws")
            hist = await ws.receive_json()
            items = hist["items"][0]["media"]
            got = {}
            for m in items:
                r = await client.get(f"/api/media/{m['id']}")
                got[m["kind"]] = (r.status, r.headers["content-type"], await r.read())
            missing = (await client.get("/api/media/nope")).status
            up = await client.post("/api/paste-image", data=PNG, headers={"content-type": "image/png", "x-filename": "My Shot!.png"})
            bad = await client.post("/api/paste-image", data=b"not a pdf", headers={"content-type": "application/pdf"})
            pdf_up = await client.post("/api/paste-image", data=pdf.read_bytes(), headers={"content-type": "application/pdf", "x-filename": "r.pdf"})
            return items, got, missing, await up.json(), bad.status, await pdf_up.json()
    items, got, missing, uploaded, bad, pdf_up = asyncio.run(go())
    assert [m["kind"] for m in items] == ["image", "pdf"] and items[1]["pages"] == 2
    assert got["image"][:2] == (200, "image/png") and got["image"][2] == PNG
    assert got["pdf"][:2] == (200, "application/pdf")
    assert missing == 404 and bad == 400
    assert uploaded["kind"] == "image" and uploaded["name"].startswith("My-Shot")
    assert pdf_up["kind"] == "pdf" and pdf_up["pages"] == 2


def test_view_image_results_carry_their_picture(tmp_path):
    pytest.importorskip("aiohttp")
    from tests.test_remote_control import make_session
    from hexiel.webui import WebState
    s = make_session(tmp_path)
    (tmp_path / "chart.png").write_bytes(PNG)
    s.messages = [{"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "view_image", "arguments": {"path": "chart.png"}}]},
                  {"role": "tool", "tool_call_id": "c1", "content": "ok"}]
    item = WebState(s).render_history()[0]
    assert item["media"][0]["kind"] == "image" and "path" not in item


def test_the_terminal_shows_a_chip_for_an_attached_file(tmp_path):
    from hexiel.tui import AttachmentChip, HexielTUI
    from tests.test_remote_control import make_session
    (tmp_path / "shot.png").write_bytes(PNG)
    make_pdf(tmp_path / "spec.pdf", 2)

    async def go():
        app = HexielTUI(make_session(tmp_path), [], {})
        async with app.run_test(size=(100, 40)) as pilot:
            app.show_attachments("compare @shot.png with @spec.pdf")
            await pilot.pause()
            return [str(c.render()) for c in app.query(AttachmentChip)]
    chips = asyncio.run(go())
    assert any("shot.png" in c for c in chips) and any("spec.pdf · 2 pages" in c for c in chips)
