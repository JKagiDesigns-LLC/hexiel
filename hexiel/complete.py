"""Path completion for the prompt: `@file ~dir ./te "quoted path/with spaces` — and plain
paths with a `/` in them — get a Tab-expandable candidate list with first-qualifier
behavior (Tab extends to the qualifier all matches share, a second Tab lists choices).
Pure functions so the behavior can be tested without a terminal."""

import os
import re
from pathlib import Path

MAX_MENU = 40                       # rows offered in the menu
MAX_SCAN = 50_000                   # give up on absurd directories instead of stalling typing
_NOISE = {".git"}                   # internal state; never offer it as a completion

_QUOTES = ('"', "'")
_BOUNDARY = ("", " ", "\t", "\n", "\r", "@")     # what may precede an opening quote


# ---------------------------------------------------------------- token detection

def find_path_token(prefix_text: str) -> str | None:
    """The path-like token ending just before the cursor (prefix_text is the text before the
    cursor). Path-like: a leading @, ~, ./, ../, /, an open quote covering the cursor, or a
    relative token that already contains a `/`. Plain single words never trigger — prose keeps
    the prompt quiet. Returns the token with its @/quote prefix, or None."""
    if not prefix_text:
        return None
    tok = _quoted_token_at_end(prefix_text)
    if tok:
        return tok
    chunk = re.split(r"[ \t\r\n]", prefix_text)[-1]
    if not chunk:
        return None
    c0 = chunk[0]
    if c0 == "@" and len(chunk) > 1:
        return chunk                           # an attachment/mention
    for lead in ("../", "./", "/"):
        if chunk.startswith(lead):
            return chunk
    if c0 == "~" and len(chunk) > 1:
        return chunk
    if "/" in chunk[1:]:                       # a relative multi-segment path, e.g. src/co
        return chunk
    return None


def _quoted_token_at_end(prefix_text: str) -> str | None:
    """An unterminated (or just-closed) quoted path whose cursor is inside/at it, e.g.
    `"my doc.a"` or `@"a b/n`. Requires the opening quote to start a fresh token (a boundary
    before it), and — for an unterminated quote — a `/` inside (or an @ before it), so prose
    apostrophes and idiom-quotes don't eat the Tab key. The token may contain spaces."""
    for q in _QUOTES:
        last = prefix_text.rfind(q)
        if last == -1:
            continue
        before = prefix_text[last - 1:last]
        if before not in _BOUNDARY:
            continue                           # mid-word apostrophe inside prose, not a path quote
        seg = prefix_text[last:]               # the quote + whatever follows up to the cursor
        inner = seg[1:]
        at = before == "@"
        wrap = "@" if at else ""
        if q not in inner:                     # still unterminated
            if "/" in inner or at:
                return f"{wrap}{seg}" if at else seg
            return None                        # `"my do` — bare quoted name: wait for a / (or @)
        if inner.endswith(q) and inner.count(q) == 1:
            return f"{wrap}{seg}" if at else seg   # just closed: the token stands as typed
        return None                            # e.g. `"a" "b` outside the pair: quirk, no help
    return None


# ---------------------------------------------------------------- candidates

def _token_parts(token: str) -> dict:
    """Split a completion token into what's typed vs what to insert: keeps the quoting/@ prefix."""
    at = False
    if token[:1] == "@":
        at = True
        token = token[1:]
    quote = token[:1] if token[:1] in _QUOTES else ""
    body = token[len(quote):]
    return {"quote": quote, "at": at, "body": body}


def _split_typed(body: str) -> tuple[str, str]:
    """(directory as typed WITH trailing slash, final-segment prefix); empty base = the cwd."""
    base, _, prefix = body.rpartition("/")
    return f"{base}/" if base else "", prefix


def completions(token: str, cwd: str, cache: dict | None = None) -> list[dict] | None:
    """Candidate entries under the directory the token points at. Each is
    {name, is_dir}; paths keep `/` separators on every OS. None when the token isn't
    completable (no such directory). `cache` (from cache_keeper()) holds directory
    listings keyed by fingerprint so per-keystroke re-typing costs no syscall."""
    parts = _token_parts(token)
    body = parts["body"]
    if not body or not body.strip("/"):        # bare "/" or "@": listing the root is noise
        return None
    base, prefix = _split_typed(body)
    try:
        expanded = os.path.expanduser(base)
        directory = Path(expanded if os.path.isabs(expanded) else os.path.join(cwd, expanded))
    except (OSError, ValueError):
        return None
    return _list_dir(directory, prefix, cache)


# listing cache: directory fingerprint -> [(name, is_dir)] — keystroke latency lives and dies here,
# since every typed letter re-filters the token's directory
_DIR_CACHE: dict[str, tuple] = {}
DIR_CACHE_MAX = 96


def cache_keeper() -> dict:
    """The cache dict callers should hold on to (one per session: fresh session, fresh cache)."""
    return _DIR_CACHE


def _list_dir(directory: Path, prefix: str, cache: dict | None = None) -> list[dict] | None:
    if not directory.is_dir():
        return None
    key = str(directory)
    sig = None
    try:
        sig = directory.stat().st_mtime_ns
        cached = cache.get(key) if cache is not None else None
        if cached is not None and cached[0] == sig:
            names = cached[1]
        else:
            if cache is not None and len(cache) >= DIR_CACHE_MAX:
                cache.pop(next(iter(cache)))       # bounded: drop the oldest (dicts are ordered)
            entries = os.scandir(directory)
            names = sorted(((e.name, e.is_dir()) for e in entries),
                           key=lambda t: (not t[1], t[0].lower()))
            if cache is not None:
                cache[key] = (sig, names)
    except OSError:
        return None
    if not names:
        return None
    if len(names) > MAX_SCAN:
        return None
    lower = prefix.lower()
    show_hidden = prefix.startswith(".")
    out = []
    for name, is_dir in names:
        if name in _NOISE:
            continue
        if not name.lower().startswith(lower):
            continue
        if not show_hidden and name.startswith("."):
            continue
        out.append({"name": name, "is_dir": is_dir})
    out.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return out[:MAX_MENU]


# ---------------------------------------------------------------- completion steps

def _common_prefix(names: list[str]) -> str:
    """Case-insensitive common prefix of several strings (the qualifier all matches share)."""
    if not names:
        return ""
    pre = names[0].lower()
    for name in names[1:]:
        low = name.lower()
        i = 0
        while i < len(pre) and i < len(low) and pre[i] == low[i]:
            i += 1
        pre = pre[:i]
        if not pre:
            break
    return pre


def _extend(token: str, name: str) -> str:
    """Token expanded so its final segment becomes `name` (directory part and prefixes stay)."""
    parts = _token_parts(token)
    typed = parts["body"].rpartition("/")[2]
    return f"{token[:len(token) - len(typed)]}{name}"


def suggest_prefix(token: str, comps: list[dict]) -> str | None:
    """First-qualifier extension: when every candidate shares a (case-insensitive) prefix
    strictly longer than what was typed, Tab extends the token to it. Returns the extended
    token or None (pop the menu instead). What was already typed keeps its case; the added
    letters take the first candidate's casing."""
    if not comps:
        return None
    parts = _token_parts(token)
    typed = parts["body"].rpartition("/")[2]
    if len(comps) == 1:
        name = comps[0]["name"]
        if name.lower() == typed.lower() and not comps[0]["is_dir"]:
            return None                        # fully typed: the finisher comes via accept_menu
        if comps[0]["is_dir"]:
            name = f"{name}/"                  # a single directory match extends navigable
        return _extend(token, name)
    shared = _common_prefix([c["name"] for c in comps])
    if not shared or len(shared) <= len(typed):
        return None
    return _extend(token, comps[0]["name"][:len(shared)])


def finish(token: str, comp: dict) -> str:
    """A fully-chosen entry's final token: directories stay navigable (trailing /); files get
    quoting fixed up (close an open quote, wrap paths with spaces in quotes) and a trailing
    space so the next word can be typed. @ and the directory segments keep their places."""
    parts = _token_parts(token)
    typed = parts["body"].rpartition("/")[2]
    head = token[:len(token) - len(typed)]        # keeps @, quote and directory segments
    name = comp["name"]
    if comp["is_dir"]:
        return f"{head}{name}/"
    if parts["quote"]:
        return f"{head}{name}{parts['quote']} "   # close the open quote
    if " " in head or " " in name:                    # a path with spaces needs quotes around
        inner = (head[1:] if parts["at"] else head) + name
        return f'@"{inner}" ' if parts["at"] else f'"{inner}" '
    return f"{head}{name} "


def autocomplete_path(prefix_text: str, cwd: str) -> dict | None:
    """What Tab should do given the text before the cursor:
    {token, value, menu: False} — extend/complete in place without a menu, or
    {token, completions: [...]} — open the choice menu. None when nothing path-ish is under
    the cursor."""
    token = find_path_token(prefix_text)
    if token is None:
        return None
    comps = completions(token, cwd)
    if not comps:
        return None
    ext = suggest_prefix(token, comps)
    if ext is None or ext == token:
        return {"token": token, "completions": comps}
    only = len(comps) == 1 and not comps[0]["is_dir"]
    return {"token": token, "value": finish(token, comps[0]) if only else ext,
            "finish": only}
