"""Clipboard images: pull a copied image off the system clipboard and save it where the
`@path` image attachment (tools.attach_images) can pick it up."""

import re
import shutil
import subprocess
import time
from pathlib import Path

_READERS = [
    (["wl-paste", "--list-types"], ["wl-paste", "--type", "{mime}"]),
    (["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
     ["xclip", "-selection", "clipboard", "-t", "{mime}", "-o"]),
]
_MIMES = ("image/png", "image/jpeg", "image/webp", "image/gif")
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif", "application/pdf": "pdf"}


def _run(cmd: list[str]) -> bytes:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return b""


def clipboard_image() -> tuple[bytes, str] | None:
    """(bytes, mime) of an image on the clipboard, or None."""
    for lister, reader in _READERS:
        if not shutil.which(lister[0]):
            continue
        types = _run(lister).decode(errors="ignore").split()
        for mime in _MIMES:
            if mime in types:
                data = _run([a.format(mime=mime) for a in reader])
                if data:
                    return data, mime
    if shutil.which("pngpaste"):                      # macOS
        data = _run(["pngpaste", "-"])
        if data:
            return data, "image/png"
    return None


def save_image(data: bytes, mime: str, cwd: str, name: str = "") -> str:
    """Write pasted or dropped bytes (an image, or a PDF) under <cwd>/.hexiel/pastes and return the path.
    A dropped file keeps a cleaned-up version of its own name so the chat reads sensibly."""
    folder = Path(cwd) / ".hexiel" / "pastes"
    folder.mkdir(parents=True, exist_ok=True)
    ext = _EXT.get(mime, "png")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name).stem).strip("-.")[:40] if name else ""
    stem = f"{stem}-" if stem else "paste-"
    path = folder / f"{stem}{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1000000}.{ext}"
    path.write_bytes(data)
    return str(path)


def paste_clipboard_image(cwd: str) -> str | None:
    found = clipboard_image()
    return save_image(*found, cwd) if found else None
