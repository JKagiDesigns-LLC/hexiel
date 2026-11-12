"""Vision for text-only models: does a model see images, and if not, replace each image in the history with
what a vision model sees in it (digest.describe), once, before the request goes out."""

from __future__ import annotations

import re

# Model families that take images. Matched against the model id, lowercased; a profile's `vision = "yes"|"no"`
# always wins over this guess.
_SEES = re.compile(
    r"claude|gpt-4o|gpt-4\.1|gpt-5|o3|o4|gemini|gemma3|gemma-3|gemma4|gemma-4|llava|bakllava|moondream|"
    r"minicpm-v|qwen.*vl|qwen2\.5vl|qwen3-vl|llama3\.2-vision|llama-3\.2-.*vision|llama4|pixtral|"
    r"mistral-small3\.[12]|mistral-medium|granite3\.2-vision|kimi-k2\.5|glm-4\.5v|glm-4v|internvl|vision")


# Families known to be text-only: their images are described. A model in neither list keeps its images (the
# old behaviour) — set `vision = "no"` on its profile to have them described instead.
_BLIND = re.compile(
    r"qwen\d?(\.\d)?-?coder|qwen3(?!.*vl)|qwq|deepseek|gpt-oss|glm-4(?:\.\d)?(?!.*v)|kimi-k2(?!\.5)|minimax|devstral|"
    r"codestral|codellama|starcoder|phi[34]|mistral-nemo|llama3\.[0-3](?!.*vision)|llama-3\.[0-3](?!.*vision)|"
    r"granite(?!.*vision)|nemotron|command-r|olmo|smollm|tinyllama|openhermes|dolphin|o1-mini|o3-mini|gpt-3\.5")


def sees_images(mc) -> bool:
    said = (getattr(mc, "vision", "") or "").strip().lower()
    if said in ("yes", "true", "1", "on"):
        return True
    if said in ("no", "false", "0", "off"):
        return False
    model = (getattr(mc, "model", "") or "").lower()
    if _SEES.search(model):
        return True
    return not _BLIND.search(model)


def describe_history(session) -> int:
    """Turn every image still in the history into text when the current model can't see images.
    Returns how many were described. A failure leaves a clear placeholder instead — never a dropped image
    the model silently ignores, never a failed turn."""
    if sees_images(session.model_config):
        return 0
    from . import digest
    question = session.__dict__.get("_turn_request", "") or ""
    done = 0
    for msg in session.messages:
        images = msg.get("images")
        if not images:
            continue
        notes = []
        for n, image in enumerate(images, 1):
            label = f"image {n}" if len(images) > 1 else "image"
            if session._stop.is_set():
                notes.append(f"[{label} not described: stopped]")
                continue
            try:
                session.on_event("note", f"👁 {session.model_config.name} can't see images — asking a vision model to describe the {label}")
                text = digest.describe(session, {k: image[k] for k in ("media_type", "data") if k in image}, question)
                notes.append(f"[{label}, described by a vision model because {session.model_config.name} is text-only]\n{text}")
                done += 1
            except Exception as exc:  # noqa: BLE001 - say why, carry on
                notes.append(f"[{label} attached but not visible to this model: {str(exc)[:200]}]")
        msg.pop("images", None)
        msg["content"] = ((msg.get("content") or "") + "\n\n" + "\n\n".join(notes)).strip()
    if done:
        session.save()
    return done


def ocr(data: bytes, timeout: float = 30.0) -> str:
    """The no-model path: the text in an image via the tesseract CLI, or "" when tesseract is missing or
    finds nothing. Image bytes go in on stdin, text comes out on stdout — no temp files."""
    import shutil
    import subprocess  # nosec B404 - a fixed argv, no shell
    exe = shutil.which("tesseract")
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "stdin", "stdout"], input=data, capture_output=True,  # nosec B603
                             timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    text = out.stdout.decode("utf-8", "replace")
    lines = [ln.rstrip() for ln in text.splitlines()]
    return "\n".join(ln for i, ln in enumerate(lines) if ln or (i and lines[i - 1])).strip()


# ---------------------------------------------------------------- no-model image card

def _barcodes(data: bytes) -> list[str]:
    """QR codes and barcodes: pyzbar when importable, else the zbarimg CLI, else none."""
    try:
        import io
        from PIL import Image
        from pyzbar.pyzbar import decode
        return [f"{c.type}: {c.data.decode('utf-8', 'replace')}" for c in decode(Image.open(io.BytesIO(data)))]
    except Exception:  # noqa: BLE001 - not installed / unreadable: try the CLI
        pass
    import shutil
    import subprocess  # nosec B404
    import tempfile
    exe = shutil.which("zbarimg")
    if not exe:
        return []
    with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as fh:
        fh.write(data)
        tmp = fh.name
    try:
        out = subprocess.run([exe, "--quiet", tmp],  # nosec B603
                             capture_output=True, timeout=20, check=False)
        lines = [ln for ln in out.stdout.decode("utf-8", "replace").splitlines() if ln.strip()]
        return [ln if ":" in ln.split(" ")[0] else f"code: {ln}" for ln in lines]
    except (OSError, subprocess.SubprocessError):
        return []
    finally:
        try:
            import os
            os.unlink(tmp)
        except OSError:
            pass


def _ocr_layout(data: bytes, timeout: float = 30.0) -> list[str]:
    """Text lines with their position (tesseract TSV): 'top-left: Save changes'. [] without tesseract."""
    import shutil
    import subprocess  # nosec B404
    exe = shutil.which("tesseract")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "stdin", "stdout", "tsv"], input=data, capture_output=True,  # nosec B603
                             timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    rows = [r.split("\t") for r in out.stdout.decode("utf-8", "replace").splitlines()[1:]]
    lines: dict = {}
    width = height = 1
    for r in rows:
        if len(r) < 12:
            continue
        level, block, par, line = r[0], r[2], r[3], r[4]
        left, top, w, h, conf, text = int(r[6]), int(r[7]), int(r[8]), int(r[9]), float(r[10]), r[11]
        if level == "1":
            width, height = max(w, 1), max(h, 1)
        if level != "5" or not text.strip() or conf < 40:
            continue
        key = (block, par, line)
        entry = lines.setdefault(key, {"x": left, "y": top, "words": []})
        entry["words"].append(text)
    out_lines = []
    for e in sorted(lines.values(), key=lambda e: (e["y"], e["x"])):
        v = "top" if e["y"] < height / 3 else "middle" if e["y"] < 2 * height / 3 else "bottom"
        h = "left" if e["x"] < width / 3 else "centre" if e["x"] < 2 * width / 3 else "right"
        out_lines.append(f"{v}-{h}: {' '.join(e['words'])}")
    return out_lines


def image_card(data: bytes) -> str:
    """What can be known about an image without any model: size, light/dark, dominant colours and which parts
    of the image hold content (Pillow), text with positions (tesseract), QR/barcodes (zbar). Each part is
    included only when its tool is installed; the card says what was missing."""
    from .tools import parse_image_size
    lines = []
    size = parse_image_size(data)
    if size:
        lines.append(f"{size[0]}×{size[1]} {size[2]}")
    missing = []
    try:
        import io
        from PIL import Image, ImageStat
        img = Image.open(io.BytesIO(data))
        img = (img.convert("RGBA") if img.mode in ("P", "LA") else img).convert("RGB")
        small = img.copy()
        small.thumbnail((600, 600))
        lum = ImageStat.Stat(small.convert("L")).mean[0]
        lines.append(f"overall {'dark' if lum < 90 else 'light' if lum > 170 else 'mid-tone'} (brightness {lum:.0f}/255)")
        pal = small.quantize(colors=6).convert("RGB")
        counts = sorted(pal.getcolors(600 * 600) or [], reverse=True)[:5]
        total = sum(c for c, _ in counts) or 1
        lines.append("dominant colours: " + ", ".join(f"#{r:02x}{g:02x}{b:02x} {100 * c // total}%" for c, (r, g, b) in counts))
        grey = small.convert("L")
        gw, gh = grey.size
        busy = []
        for row, vname in enumerate(("top", "middle", "bottom")):
            for col, hname in enumerate(("left", "centre", "right")):
                box = grey.crop((col * gw // 3, row * gh // 3, (col + 1) * gw // 3, (row + 1) * gh // 3))
                if ImageStat.Stat(box).stddev[0] > 9:
                    busy.append(f"{vname}-{hname}")
        lines.append("content in: " + (", ".join(busy) if busy else "nowhere distinct (a flat image)"))
    except ImportError:
        missing.append("Pillow (colours/layout)")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"(could not decode the image: {exc})")
    text = _ocr_layout(data)
    if text:
        lines.append("text (tesseract, with position):")
        lines.extend("  " + t for t in text[:80])
    elif not __import__("shutil").which("tesseract"):
        missing.append("tesseract (text)")
    codes = _barcodes(data)
    if codes:
        lines.append("codes: " + "; ".join(codes))
    head = "[image card — no vision model was used"
    if missing:
        head += "; not installed: " + ", ".join(missing)
    return head + "]\n" + "\n".join(lines)


# ---------------------------------------------------------------- PDFs whose pages are pictures

def pdf_text(path, session=None, max_render: int = 8, min_chars: int = 25) -> list[tuple[int, str, str]]:
    """[(page number, how, text)] — the text layer where there is one; a page without one (a scan, a photo) is
    rendered with pdftoppm and read by a vision model (when a session is bound), else tesseract, else an image
    card. At most `max_render` pages are rendered per call (the rest say so)."""
    import shutil
    import subprocess  # nosec B404
    import tempfile
    from pathlib import Path
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    out, rendered = [], 0
    exe = shutil.which("pdftoppm")
    for n, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - a broken page: treat as an image
            text = ""
        if len(text.strip()) >= min_chars:
            out.append((n, "text", text))
            continue
        if not exe:
            out.append((n, "empty", "[no text layer on this page; install poppler (pdftoppm) to have it read as an image]"))
            continue
        if rendered >= max_render:
            out.append((n, "empty", f"[no text layer; not rendered — over {max_render} rendered pages this call]"))
            continue
        rendered += 1
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "p"
            try:
                subprocess.run([exe, "-png", "-r", "110", "-f", str(n), "-l", str(n), "-singlefile", str(path), str(base)],  # nosec B603
                               capture_output=True, timeout=60, check=True)
                data = base.with_suffix(".png").read_bytes()
            except (OSError, subprocess.SubprocessError):
                out.append((n, "empty", "[no text layer; rendering the page failed]"))
                continue
        out.append((n, *read_picture(data, session, "Transcribe all text on this document page, in reading order; "
                                                      "describe figures and tables briefly.")))
    return out


def read_picture(data: bytes, session=None, question: str = "") -> tuple[str, str]:
    """(how, text) for an image: a vision model when a session can reach one, else OCR, else the image card."""
    import base64
    if session is not None:
        try:
            from . import digest
            from .tools import image_payload
            payload = image_payload(data)
            if payload:
                text = digest.describe(session, payload[0], question)
                if text and not text.startswith("[no vision model was usable"):
                    return "vision", text
        except Exception:  # noqa: BLE001 - next way
            pass
    text = ocr(data)
    if text:
        return "ocr", text
    return "card", image_card(data)


# ---------------------------------------------------------------- render-and-look

BROWSERS = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome", "microsoft-edge",
            "msedge", "brave-browser", "firefox")


def find_browser() -> str | None:
    import shutil
    for name in BROWSERS:
        exe = shutil.which(name)
        if exe:
            return exe
    if __import__("sys").platform == "darwin":
        from pathlib import Path
        for app in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "/Applications/Chromium.app/Contents/MacOS/Chromium"):
            if Path(app).exists():
                return app
    return None


def markdown_to_html(text: str) -> str:
    """Markdown → HTML for rendering: python-markdown or pandoc when present, else a small built-in converter
    (headings, lists, code blocks, emphasis, links, tables as preformatted)."""
    try:
        import markdown
        return markdown.markdown(text, extensions=["tables", "fenced_code"])
    except ImportError:
        pass
    import html
    import re
    out, in_code, in_list = [], False, False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            out.append("</pre>" if in_code else "<pre>")
            in_code = not in_code
            continue
        if in_code:
            out.append(html.escape(line))
            continue
        esc = html.escape(line)
        esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
        esc = re.sub(r"`([^`]+)`", r"<code>\1</code>", esc)
        esc = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', esc)
        m = re.match(r"^(#{1,6})\s+(.*)", esc)
        bullet = re.match(r"^\s*[-*]\s+(.*)", esc)
        if bullet and not in_list:
            out.append("<ul>")
            in_list = True
        if not bullet and in_list:
            out.append("</ul>")
            in_list = False
        if m:
            out.append(f"<h{len(m.group(1))}>{m.group(2)}</h{len(m.group(1))}>")
        elif bullet:
            out.append(f"<li>{bullet.group(1)}</li>")
        elif esc.strip():
            out.append(f"<p>{esc}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def render(target: str, cwd: str, width: int = 1280, height: int = 900, wait_ms: int = 3000) -> bytes:
    """A PNG of a page as a browser draws it: a local .html/.htm/.md/.svg file or an http(s) URL (headless
    Chromium-family browser, or Firefox). Raises RuntimeError with the reason."""
    import os
    import subprocess  # nosec B404
    import tempfile
    from pathlib import Path
    exe = find_browser()
    if not exe:
        raise RuntimeError("no browser found to render with — install Chromium, Chrome, Edge or Firefox")
    width, height = max(320, min(int(width), 3840)), max(240, min(int(height), 4000))
    with tempfile.TemporaryDirectory() as tmp:
        if re.match(r"^https?://", target):
            url = target
        else:
            p = Path(target) if Path(target).is_absolute() else Path(cwd) / target
            if not p.is_file():
                raise RuntimeError(f"no such file: {target}")
            if p.suffix.lower() in (".md", ".markdown"):
                page = Path(tmp) / "page.html"
                page.write_text("<!doctype html><meta charset=utf-8><style>body{font:15px/1.5 system-ui,sans-serif;"
                                "max-width:860px;margin:32px auto;padding:0 16px}pre{background:#f4f4f4;padding:8px;"
                                "overflow:auto}</style>" + markdown_to_html(p.read_text(errors="replace")))
                url = page.as_uri()
            else:
                url = p.resolve().as_uri()
        png = Path(tmp) / "shot.png"
        if "firefox" in os.path.basename(exe):
            argv = [exe, "--headless", "--window-size", f"{width},{height}", "--screenshot", str(png), url]
        else:
            argv = [exe, "--headless=new", "--hide-scrollbars", "--disable-gpu", f"--window-size={width},{height}",
                    f"--screenshot={png}", f"--virtual-time-budget={int(wait_ms)}", url]
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                argv.insert(1, "--no-sandbox")
        try:
            subprocess.run(argv, capture_output=True, timeout=90, check=False, cwd=tmp)  # nosec B603
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"the browser failed: {exc}") from None
        if not png.exists():
            raise RuntimeError("the browser produced no screenshot")
        return png.read_bytes()
