"""Images and PDFs the user can *see*: pasted, dropped or mentioned by path in a message, or shown by the
view_image tool. The model gets the image through tools.attach_images; this module is for the people —
the terminal shows a thumbnail or a clickable chip, the web UI a thumbnail or a PDF viewer.

Only files that surfaced in the conversation are ever served: each gets an id when it is registered, and the web
UI's /api/media/<id> serves nothing else (the relay would otherwise expose arbitrary files)."""

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .tools import IMAGE_EXTS, MAX_ATTACH_BYTES, parse_image_size

VIEW_EXTS = IMAGE_EXTS + (".pdf",)
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
        ".webp": "image/webp", ".pdf": "application/pdf"}
_PATH_TOKEN = re.compile(r"""@?(?:"([^"]+)"|'([^']+)'|(\S+))""")
_registry: dict[str, Path] = {}


def kind_of(path: Path | str) -> str | None:
    ext = Path(path).suffix.lower()
    return "pdf" if ext == ".pdf" else "image" if ext in IMAGE_EXTS else None


def pdf_pages(path: Path) -> int | None:
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(path)).pages)
    except Exception:  # noqa: BLE001 - unreadable or encrypted: just don't say a page count
        return None


def register(path: Path | str) -> str | None:
    """Make a file servable and return its id (stable for the same file); None if it is not a viewable file."""
    path = Path(path).expanduser()
    try:
        if kind_of(path) is None or not path.is_file() or path.stat().st_size > MAX_ATTACH_BYTES:
            return None
        key = f"{path.resolve()}:{path.stat().st_mtime_ns}"
    except OSError:
        return None
    media_id = hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()[:16]
    _registry[media_id] = path.resolve()
    return media_id


def resolve(media_id: str) -> Path | None:
    path = _registry.get(media_id)
    return path if path is not None and path.is_file() else None


def describe(path: Path | str) -> dict | None:
    """What the UIs show for a file: {kind, name, path, id, w, h, pages, size}."""
    path = Path(path).expanduser()
    media_id = register(path)
    if media_id is None:
        return None
    info = {"kind": kind_of(path), "name": path.name, "path": str(path.resolve()), "id": media_id,
            "size": path.stat().st_size}
    if info["kind"] == "image":
        try:
            probe = parse_image_size(path.read_bytes())
        except OSError:
            probe = None
        if probe:
            info["w"], info["h"] = probe[0], probe[1]
    else:
        info["pages"] = pdf_pages(path)
    return info


def attachments_in(text: str, cwd: str) -> list[dict]:
    """Image and PDF paths mentioned in a message (`@shot.png`, `~/Downloads/a.pdf`, quoted paths with spaces)."""
    found, seen = [], set()
    for m in _PATH_TOKEN.finditer(text):
        raw = (m.group(1) or m.group(2) or m.group(3) or "").rstrip(",.;:!?)")
        if not raw.lower().endswith(VIEW_EXTS):
            continue
        path = Path(os.path.expanduser(raw))
        path = path if path.is_absolute() else Path(cwd) / path
        if path in seen:
            continue
        seen.add(path)
        info = describe(path)
        if info:
            found.append(info)
    return found


def label(info: dict) -> str:
    """One line for a terminal chip."""
    if info["kind"] == "pdf":
        pages = f" · {info['pages']} page{'s' if info['pages'] != 1 else ''}" if info.get("pages") else ""
        return f"📄 {info['name']}{pages}"
    dims = f" · {info['w']}×{info['h']}" if info.get("w") else ""
    return f"🖼 {info['name']}{dims}"


def open_external(path: Path | str) -> bool:
    """Open a file in the system viewer (the terminal can't show a PDF)."""
    path = str(path)
    try:
        if sys.platform == "win32":
            os.startfile(path)                                   # noqa: S606 - a file the user attached
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])                     # noqa: S603,S607
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603
        else:
            return False
        return True
    except OSError:
        return False


def pdf_preview(path: Path | str) -> Path | None:
    """A PNG of a PDF's first page for the terminal (needs poppler's pdftoppm; None without it)."""
    exe = shutil.which("pdftoppm")
    path = Path(path)
    if not exe:
        return None
    from .config import home_data_dir
    out_dir = home_data_dir() / "previews"
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / hashlib.sha1(f"{path.resolve()}:{path.stat().st_mtime_ns}".encode(), usedforsecurity=False).hexdigest()[:16]
    png = base.with_suffix(".png")
    if not png.exists():
        try:
            subprocess.run([exe, "-png", "-f", "1", "-l", "1", "-singlefile", "-scale-to", "720", str(path), str(base)],
                           capture_output=True, timeout=20, check=True)               # noqa: S603
        except (OSError, subprocess.SubprocessError):
            return None
    return png if png.exists() else None
