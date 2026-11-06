"""Desktop tools for the machine hexiel runs on — screenshot capture, the system
clipboard, and OCR. Loaded as the `desktop` toolset (auto-detected whenever a
display server is present; headless/ssh runs skip it) or pulled in on demand
with the load_tools tool.

Everything uses whatever helper programs the machine already has — grim on a
Wayland compositor, scrot/import under X11, screencapture on macOS, tesseract
for OCR — and reports exactly what to install when nothing is there, so these
never turn into dependency problems."""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import clipboard
from .config import home_data_dir
from .tools import MAX_OUTPUT, ToolDef, ToolResult, text_result

obj = lambda **props: {"type": "object", "properties": props}  # noqa: E731


# ---------------------------------------------------------------- screenshot

# (which, command) tried in order. {out} is the capture path.
_CAPTURES = [
    ("wayland", ["grim", "{out}"]),
    ("wayland", ["gnome-screenshot", "-f", "{out}"]),
    ("wayland", ["spectacle", "-b", "-n", "-o", "{out}"]),
    ("macos", ["screencapture", "-x", "{out}"]),
    ("x11", ["scrot", "-o", "{out}"]),
    ("x11", ["maim", "{out}"]),
    ("x11", ["import", "-window", "root", "{out}"]),
]


def _desktop_kind() -> str:
    if shutil.which("screencapture"):
        return "macos"
    if shutil.which("grim") or shutil.which("gnome-screenshot") or shutil.which("spectacle"):
        return "wayland"
    if shutil.which("scrot") or shutil.which("maim") or shutil.which("import"):
        return "x11"
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    return "x11"


def _has_desktop() -> bool:
    """A display server this process can see: macOS, Wayland or X11. False on a
    headless box, where the desktop tools would only be dead weight."""
    if sys.platform == "darwin" or shutil.which("screencapture"):
        return True
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))


def _screenshot(args, ctx) -> ToolResult:
    from .tools import image_payload
    out = Path(home_data_dir() / "screenshots")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000}.png"
    for _kind, cmd in _CAPTURES:
        if not shutil.which(cmd[0]):
            continue
        argv = [a.format(out=str(path)) for a in cmd]
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            return text_result(f"error running {cmd[0]}: {exc}", True)
        if proc.returncode == 0 and path.is_file() and path.stat().st_size > 0:
            data = path.read_bytes()
            payload = image_payload(data)
            line = f"{path}  ({len(data) // 1024} KB)"
            if payload:
                return ToolResult(text=line, images=[payload[0]])
            return text_result(line + "\n  (could not attach the capture to the conversation)")
        path.unlink(missing_ok=True)
    hint = {"macos": "screencapture is missing",
            "wayland": "no Wayland screenshot helper found (install grim, or the distro's gnome-screenshot/spectacle)",
            "x11": "no X11 screenshot helper found (install scrot, or imagemagick's import)"}[_desktop_kind()]
    return text_result(f"error: screenshot failed — {hint}", True)


# ---------------------------------------------------------------- clipboard

def _clipboard(args, ctx) -> ToolResult:
    op = args.get("op", "read")
    if op == "read":
        text = clipboard.clipboard_text()
        if text is None:
            hint = clipboard.missing_helper_hint() or "wl-clipboard / xclip / pbpaste"
            return text_result(f"error: nothing on the clipboard (or no helper installed — {hint})", True)
        note = "" if len(text) < MAX_OUTPUT else f"[{len(text) - MAX_OUTPUT} more characters cut locally]"
        return text_result(text[:MAX_OUTPUT] + note)
    if op == "write":
        text = str(args.get("text", ""))
        if clipboard.copy_text(text):
            return text_result(f"copied {len(text):,} characters to the clipboard")
        return text_result("error: no clipboard helper found — " + clipboard.missing_helper_hint(), True)
    return text_result("error: op must be 'read' or 'write'", True)


# ---------------------------------------------------------------- ocr

_TESS_INSTALL = ("tesseract is not installed — 'sudo pacman -S tesseract' (Arch), "
                 "'sudo apt install tesseract-ocr' (Debian/Ubuntu), 'brew install tesseract' (macOS)")

def _ocr(args, ctx) -> ToolResult:
    from .tools import _resolve_path
    path = _resolve_path(str(args.get("path", "")), ctx)
    if not path.is_file():
        return text_result(f"error: {path} does not exist", True)
    tess = shutil.which("tesseract")
    if not tess:
        return text_result("error: " + _TESS_INSTALL, True)
    langs = str(args.get("langs", "")).strip()
    cmd = [*([tess, str(path), "stdout", "-l", langs] if langs else [tess, str(path), "stdout"])]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return text_result(f"error running tesseract: {exc}", True)
    text = proc.stdout.strip()
    if not text and proc.returncode != 0:
        return text_result(f"error: tesseract failed: {(proc.stderr or '').strip()[:400]}", True)
    if not text:
        return text_result(f"{path}: tesseract found no text in this image")
    lines = text.splitlines()
    shown = len(lines)
    if shown > 400:
        text = "\n".join(lines[:400]) + f"\n…[{shown - 400} more lines cut locally; use the read tool on the saved output below]"
        save = Path(home_data_dir() / "ocr") / f"{time.strftime('%Y%m%d-%H%M%S')}.txt"
        save.parent.mkdir(parents=True, exist_ok=True)
        save.write_text("\n".join(lines))
        text += f"\n(full text saved to {save})"
    return text_result(f"{path}\n  ≈{len(text):,} characters of recognized text:\n{text}")


def desktop_tools() -> list[ToolDef]:
    return [
        ToolDef("screenshot", "Capture the screen (or the current monitor) and attach the image "
                "to the conversation so a vision-capable model can look at it. Use when the user "
                "shows or mentions something visible on screen.",
                obj(), readonly=True, run=_screenshot),
        ToolDef("clipboard", "Read text from or write text to the system clipboard. "
                "op=read returns whatever text the clipboard holds; op=write puts `text` on it.",
                obj(op={"type": "string", "enum": ["read", "write"]},
                    text={"type": "string"}),
                run=_clipboard),
        ToolDef("ocr", "Extract the text from an image file (screenshot, photo, scan) with "
                "tesseract. For PDFs and Word files use the docs tools instead.",
                obj(path={"type": "string"}, langs={"type": "string"}),
                run=_ocr),
    ]
