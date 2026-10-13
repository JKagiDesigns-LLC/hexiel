"""Clipboard images: pull a copied image off the system clipboard and save it where the
`@path` image attachment (tools.attach_images) can pick it up."""

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
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


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


def save_image(data: bytes, mime: str, cwd: str) -> str:
    """Write pasted image bytes under <cwd>/.hexiel/pastes and return the path."""
    folder = Path(cwd) / ".hexiel" / "pastes"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"paste-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1000000}.{_EXT.get(mime, 'png')}"
    path.write_bytes(data)
    return str(path)


def paste_clipboard_image(cwd: str) -> str | None:
    found = clipboard_image()
    return save_image(*found, cwd) if found else None
