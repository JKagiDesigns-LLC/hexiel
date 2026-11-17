"""No-model image card, PDFs without a text layer, and render-and-look."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import vision
from hexiel.config import ModelConfig
from tests.test_symbols import session_in

def _hello_pdf(text: str = "Quarterly invoice total 1234 USD") -> bytes:
    """A one-page PDF with a real text layer and a correct xref table."""
    stream = f"BT /F1 18 Tf 20 80 Td ({text}) Tj ET".encode()
    objs = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 400 144]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
            b"<</Length %d>>stream\n" % len(stream) + stream + b"\nendstream",
            b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


HELLO_PDF = _hello_pdf()


def png(tmp_path, colour="red", size="200x120"):
    out = tmp_path / f"{colour}.png"
    subprocess.run(["magick", "-size", size, f"xc:{colour}", str(out)], check=True)
    return out


needs_magick = pytest.mark.skipif(not shutil.which("magick"), reason="ImageMagick not installed")


@needs_magick
def test_image_card_without_any_model(tmp_path):
    pytest.importorskip("PIL")
    card = vision.image_card(png(tmp_path, "white").read_bytes())
    assert card.startswith("[image card — no vision model was used")
    assert "200×120 png" in card and "overall light" in card and "#ffffff" in card
    assert "nowhere distinct" in card


@pytest.mark.skipif(not (shutil.which("qrencode") and shutil.which("zbarimg")), reason="qrencode/zbar not installed")
def test_image_card_reads_qr_codes(tmp_path):
    qr = tmp_path / "qr.png"
    subprocess.run(["qrencode", "-s", "6", "-o", str(qr), "https://hexiel.tech"], check=True)
    assert "QR-Code:https://hexiel.tech" in vision.image_card(qr.read_bytes())


def test_pdf_with_a_text_layer_is_read_as_text(tmp_path):
    f = tmp_path / "invoice.pdf"
    f.write_bytes(HELLO_PDF)
    pages = vision.pdf_text(f)
    assert pages[0][1] == "text" and "Quarterly invoice total 1234" in pages[0][2]


@needs_magick
@pytest.mark.skipif(not shutil.which("pdftoppm"), reason="poppler not installed")
def test_scanned_pdf_pages_are_rendered_and_read(tmp_path, monkeypatch):
    scan = tmp_path / "scan.pdf"
    subprocess.run(["magick", str(png(tmp_path, "navy")), str(scan)], check=True)
    seen = []
    monkeypatch.setattr(vision, "read_picture", lambda data, session=None, q="": seen.append(data[:8]) or ("vision", "a navy page"))
    pages = vision.pdf_text(scan)
    assert pages == [(1, "vision", "a navy page")] and seen[0] == b"\x89PNG\r\n\x1a\n"
    # and through the read tool
    s = session_in(tmp_path)
    out = next(t for t in s.tools if t.name == "read").run({"path": "scan.pdf"}, s.tool_ctx).text
    assert "page 1 (no text layer — read by a vision model)" in out and "a navy page" in out


def test_read_picture_falls_back_to_ocr_then_card(monkeypatch):
    monkeypatch.setattr(vision, "ocr", lambda data, timeout=30.0: "TOTAL 42")
    assert vision.read_picture(b"x") == ("ocr", "TOTAL 42")
    monkeypatch.setattr(vision, "ocr", lambda data, timeout=30.0: "")
    monkeypatch.setattr(vision, "image_card", lambda data: "[image card]")
    assert vision.read_picture(b"x") == ("card", "[image card]")


def test_markdown_fallback_converter(monkeypatch):
    monkeypatch.setitem(sys.modules, "markdown", None)          # python-markdown is a dependency now: force the fallback
    html = vision.markdown_to_html("# Title\n\n- **one**\n- `two`\n\n```\ncode <x>\n```\n")
    assert "<h1>Title</h1>" in html and "<li><b>one</b></li>" in html and "&lt;x&gt;" in html


@pytest.mark.skipif(not vision.find_browser(), reason="no browser")
def test_render_shows_the_page_or_describes_it(tmp_path, monkeypatch):
    (tmp_path / "page.html").write_text("<body style='background:#c00'><h1>Hello</h1></body>")
    s = session_in(tmp_path)
    render = next(t for t in s.tools if t.name == "render")
    s.model_config = ModelConfig(name="v", provider="anthropic", model="claude-sonnet-5-5")
    out = render.run({"target": "page.html", "width": 400, "height": 300}, s.tool_ctx)
    assert not out.is_error and out.images and "400x300" in out.text.replace("×", "x")
    s.model_config = ModelConfig(name="t", provider="openai", model="qwen3-coder:30b")
    monkeypatch.setattr(vision, "read_picture", lambda data, session=None, q="": ("card", "a red page"))
    out = render.run({"target": "page.html", "width": 400, "height": 300}, s.tool_ctx)
    assert not out.images and "a red page" in out.text and "can't see images" in out.text
    assert render.run({"target": "missing.html"}, s.tool_ctx).is_error
