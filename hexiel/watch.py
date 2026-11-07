"""/watch — hexiel stays alive and reacts to AI:/AI? comments saved in any editor.

You write a note in whatever file you have open —

    # AI: move the login endpoint to /api/v2
    // AI? should this module keep the cache?

— save, and hexiel acts on it like you typed the comment in the chat
(`AI:` = an instruction; `AI?` = a question it should answer). Works in every
language: the decoration (# // <!-- /*) is stripped around the marker, only the
words after `AI:` count. A comment reacts just once — reword it (or move it)
and it reacts again. `/watch` again, or /watch off, stops watching.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
             "env", "dist", "build", "target", ".mypy_cache", ".ruff_cache",
             ".pytest_cache", ".tox", "site-packages", ".next", ".nuxt"}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip",
                   ".gz", ".tgz", ".xz", ".7z", ".woff", ".woff2", ".ttf", ".otf",
                   ".eot", ".mp3", ".mp4", ".mov", ".avi", ".wav", ".sqlite", ".db",
                   ".bin", ".exe", ".dll", ".so", ".dylib", ".pyc", ".class", ".o"}
TICK = 1.5                    # seconds between passes: a save reacts within this
MAX_FILE = 512 * 1024         # a save that big is an input artifact, not a note


def comment_markers(text: str) -> list[tuple[int, str, str]]:
    """Every AI-marker line in one file: [(line_no (1-based), kind ':'|'?', text)]."""
    found = []
    for no, line in enumerate(text.splitlines(), 1):
        if len(line) > 400:                              # a very long line isn't a note
            continue
        hit = re.search(r"(?<![A-Za-z0-9])AI\s*([?:])", line)
        if not hit:
            continue
        kind = hit.group(1)
        after = line[hit.end():].strip()
        after = re.sub(r"(?:\*/|-->|--+|%+)\s*$", "", after).strip()  # trailing close (*/, -->, --)
        after = after.strip("—-")                        # leading separator glue
        if not after:
            continue
        found.append((no, kind, after))
    return found


class Watcher:
    """One /watch: scans a directory tree on a slow tick for new AI-comments and
    hands each one to `on_comment(path, line_no, kind, text)` exactly once. Pure
    polling (no fs events) — cheap and portable; state is a per-file fingerprint."""

    def __init__(self, root: str, on_comment, tick: float = TICK):
        self.root = Path(root)
        self.on_comment = on_comment
        self.tick = tick
        self._sigs: dict[str, tuple[int, int]] = {}       # path -> (mtime_ns, size)
        self._seen: set[tuple[str, str]] = set()          # (path, text) reacted already
        self._stop = threading.Event()

    # ---------------------------------------------------------------- scanning

    def _walk(self) -> list[Path]:
        out = []
        root = self.root
        stack = [root]
        while stack and len(out) < 4000:                  # never scan forever on a big tree
            top = stack.pop()
            try:
                entries = sorted(top.iterdir())
            except OSError:
                continue
            for e in entries:
                if e.is_dir():
                    if e.name not in SKIP_DIRS and not e.name.startswith(".git"):
                        stack.append(e)
                elif e.is_file() and e.suffix.lower() not in BINARY_SUFFIXES:
                    out.append(e)
        return out

    def scan_once(self) -> int:
        """One pass. Returns how many new comments fired (on_comment calls)."""
        fired = 0
        for path in self._walk():
            key = str(path)
            try:
                sig = (path.stat().st_mtime_ns, path.stat().st_size)
            except OSError:
                continue
            if sig == self._sigs.get(key):
                continue                                  # unchanged: no re-read, no re-parse
            self._sigs[key] = sig
            try:
                if path.stat().st_size > MAX_FILE:
                    continue
            except OSError:
                continue
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            for no, kind, entry in comment_markers(text):
                mark = (key, entry)
                if mark not in self._seen:
                    self._seen.add(mark)
                    fired += 1
                    try:
                        self.on_comment(path, no, kind, entry)
                    except Exception:  # noqa: BLE001 - one bad callback must not kill the watcher
                        pass
        # files that vanished stay remembered (their keys cannot re-fire anyway)
        return fired

    def run(self) -> None:
        while not self._stop.is_set():
            self.scan_once()
            self._stop.wait(self.tick)

    def start_background(self) -> None:
        threading.Thread(target=self.run, daemon=True, name="hexiel-watch").start()

    def stop(self) -> None:
        self._stop.set()

    # ---------------------------------------------------------------- text for a turn

    def prompt_for(self, path: Path, line_no: int, kind: str, text: str) -> str:
        rel = path.relative_to(self.root) if self._within_root(path) else path
        word = "answer this question" if kind == "?" else "do this"
        return (f"[watch while you were idle — saved in {rel}:{line_no}]\n"
                f"You noticed a comment there. {word}: {text}")

    def _within_root(self, path: Path) -> bool:
        try:
            path.relative_to(self.root)
            return True
        except ValueError:
            return False