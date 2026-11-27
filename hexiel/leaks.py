"""Tool calls a model wrote into its reply text must never reach a screen.

Some models (DeepSeek through certain gateways, glm-flash as a fallback) sometimes write their tool call as text —
`<｜DSML｜parameter name="command">…`, or `[tool call] bash({...})` — instead of using the tool channel. session.py
already turns such text back into a real call; this module keeps it off every client (terminal, browser, phone,
relay) while it streams, and out of the saved chat afterwards.

LeakFilter sits between the model's stream and the clients: it passes prose through, holds back a tail that might be
the start of a marker until the next chunk shows what it is, and from the first real marker on swallows the rest.
It works however the stream is chunked, down to one character at a time."""

from __future__ import annotations

import re

# the DSML tag as models and gateways write it: full-width or ASCII bars, spaces tolerated
_DSML = re.compile(r"<\s*[|｜]\s*DSML")
_TOOL_LINE = re.compile(r"(?m)^[ \t]*\[tool call\][ \t]*[A-Za-z_][\w.-]*[ \t]*\(")
# a tail that could still become a marker: held back (never lost) until the next chunk decides
_DSML_PREFIX = re.compile(r"<\s*(?:[|｜]\s*(?:D(?:S(?:M(?:L)?)?)?)?)?\s*$")
_TOOL_PREFIX = "[tool call]"


def _first_marker(text: str) -> int:
    hits = [m.start() for m in (_DSML.search(text), _TOOL_LINE.search(text)) if m]
    return min(hits) if hits else -1


def has_leak(text: str) -> bool:
    return _first_marker(text or "") >= 0


def strip_leaks(text: str) -> str:
    """The prose before the first leaked call; everything from the marker on is dropped."""
    text = text or ""
    cut = _first_marker(text)
    return text if cut < 0 else text[:cut].rstrip()


def _held_tail(buf: str) -> int:
    """Index where a possibly-incomplete marker starts at the end of buf, or len(buf) when nothing needs holding."""
    m = _DSML_PREFIX.search(buf)
    if m:
        return m.start()
    line_start = buf.rfind("\n") + 1
    tail = buf[line_start:].lstrip(" \t")
    if tail and (_TOOL_PREFIX.startswith(tail.lower()) or re.match(r"\[tool call\][ \t]*[\w.-]*[ \t]*$", tail, re.I)):
        return line_start + (len(buf[line_start:]) - len(tail))
    return len(buf)


class LeakFilter:
    def __init__(self) -> None:
        self.suppressed = False        # a leaked call was seen: the rest of this reply is not shown
        self._held = ""

    def feed(self, chunk: str) -> str:
        """The part of this chunk that is safe to show."""
        if self.suppressed or not chunk:
            return ""
        buf = self._held + chunk
        cut = _first_marker(buf)
        if cut >= 0:
            self.suppressed, self._held = True, ""
            return buf[:cut].rstrip(" \t") if buf[:cut].strip() else ""
        keep = _held_tail(buf)
        self._held = buf[keep:]
        return buf[:keep]

    def flush(self) -> str:
        """End of the reply: anything held back turned out to be ordinary text."""
        out, self._held = ("" if self.suppressed else self._held), ""
        return out
