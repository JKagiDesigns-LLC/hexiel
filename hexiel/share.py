"""/share — a self-contained single-file HTML transcript of this conversation.

No server, no external assets: css inline, everything escaped, secrets scrubbed
with the same redactor the saved session uses, and the per-turn fresh-facts
bookkeeping stripped out. Tool activity reads as collapsed steps so the file
captures how hexiel worked, not just what it said. The file lands next to the
session's history (data dir sessions/<id>.html); open or attach it anywhere.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from .session import STEERING_NOTE, strip_env_note
from .config import home_data_dir

TOOL_CLIP = 2000          # a tool result keeps at most this many chars in the share

# ---------------------------------------------------------------- tiny md → html

_BLOCK_RE = re.compile(r"^\s*((?:[-*+]|\d+[.)]))\s+(.*)$")


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*\s][^*]*)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)
    return text


def md_to_html(text: str) -> str:
    """The assistant writes markdown; the browser gets a faithful-but-small
    rendering: fenced code, headings, lists, blockquotes, paragraphs."""
    out: list[str] = []
    fence: list[str] = []
    fence_open = False
    para: list[str] = []
    open_tag = ""                                     # the <ul>/<ol> currently open

    def flush_para() -> None:
        nonlocal para
        if para:
            out.append(f"<p>{_inline('\n'.join(para))}</p>")
            para = []

    def close_list() -> None:
        nonlocal open_tag
        if open_tag:
            out.append(f"</{open_tag}>")
            open_tag = ""

    for line in text.splitlines():
        if line.strip().startswith("```"):
            if fence_open:
                out.append(f"<pre><code>{html.escape('\n'.join(fence), quote=False)}</code></pre>")
                fence = []
                fence_open = False
            else:
                close_list()
                flush_para()
                fence_open = True                     # the opening line (language tag) is dropped
            continue
        if fence_open:
            fence.append(line)
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            close_list()
            flush_para()
            n = len(m.group(1))
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
            continue
        m = _BLOCK_RE.match(line)
        if m:
            flush_para()
            tag = "ol" if m.group(1)[0].isdigit() else "ul"
            if open_tag != tag:
                close_list()
                out.append(f"<{tag}><li>{_inline(m.group(2))}</li>")
                open_tag = tag
            else:
                out.append(f"<li>{_inline(m.group(2))}</li>")
            continue
        if line.startswith("> "):
            close_list()
            flush_para()
            out.append(f"<blockquote><p>{_inline(line[2:])}</p></blockquote>")
            continue
        if not line.strip():
            close_list()
            flush_para()
            continue
        para.append(line)
    if fence_open:
        out.append(f"<pre><code>{html.escape('\n'.join(fence), quote=False)}</code></pre>")
    close_list()
    flush_para()
    return "\n".join(out)

# ---------------------------------------------------------------- transcript body


def _call_desc(call: dict) -> str:
    """'read <file> pattern=…' style one-line description of a tool call."""
    name = str(call.get("name", "tool"))
    args = call.get("arguments") or {}
    bits = []
    for key in ("path", "file", "pattern", "query", "url", "cmd", "code", "name", "description"):
        if key in args:
            v = str(args[key]).replace("\n", " ⏎ ")
            bits.append(v[:120])
    return f"{name} " + " ".join(bits)[:160]


def _bubble(role_class: str, who: str, body: str, ts: str) -> str:
    when = f"<time>{ts}</time>" if ts else ""
    return (f'<div class="msg {role_class}"><div class="head"><span class="who">{who}</span>{when}</div>'
            f'<div class="body">{body}</div></div>')


def _tool_step(call: dict, results: dict) -> str:
    desc = _call_desc(call)
    got = results.get(call.get("id"))
    if got is None:
        result, err = "(no result captured)", False
    else:
        result, err = got
    if len(result) > TOOL_CLIP:
        result = result[:TOOL_CLIP] + f"\n… [{len(result) - TOOL_CLIP} more chars]"
    err = " err" if err else ""
    return (f'<details class="tool{err}"><summary>⚙ {_inline(desc)}</summary>'
            f"<pre>{html.escape(result, quote=False)}</pre></details>")


def transcript_html(session) -> str:
    """The conversation as HTML: your turns, hexiel's answers, tool steps folded closed;
    compaction markers shown as dividers. Fresh-facts bookkeeping and mid-turn steering
    prefixes never appear."""
    import datetime
    msgs = session.messages
    results: dict[str, tuple[str, bool]] = {}
    for m in msgs:                                    # tool results paired by id, pre-computed
        if m.get("role") == "tool" and m.get("tool_call_id"):
            results[m["tool_call_id"]] = (str(m.get("content", "")), bool(m.get("is_error")))
    chat: list[str] = []
    for m in msgs:
        role = m.get("role")
        ts = ""
        if m.get("ts"):
            try:
                ts = datetime.datetime.fromtimestamp(m["ts"]).astimezone().strftime("%H:%M")
            except (TypeError, ValueError, OSError):
                ts = ""
        if role == "user":
            body = strip_env_note(str(m.get("content", ""))).removeprefix(STEERING_NOTE).strip()
            if body.startswith("<auto-compaction>"):
                chat.append('<div class="divider">— earlier history auto-compacted —</div>')
            elif body:
                chat.append(_bubble("user", "you", md_to_html(body), ts))
        elif role == "assistant":
            parts = []
            text = m.get("content", "")
            if text.strip():
                parts.append(f'<div class="md">{md_to_html(text)}</div>')
            parts += [_tool_step(c, results) for c in m.get("tool_calls", [])]
            if parts:
                chat.append(_bubble("hexiel", "hexiel", "\n".join(parts), ts))
        # role == tool: folded into the call that produced it; role == graphic: card skipped
    return "\n".join(chat)


# ---------------------------------------------------------------- export


CSS = """
:root{--bg:#181a1f;--fg:#d6dae2;--accent:#c8b6ff;--line:#2b2e36;--muted:#8b93a7}
*{box-sizing:border-box}body{background:var(--bg);color:var(--fg);margin:0 auto;max-width:860px;
  font:15px/1.6 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding:24px 16px 64px}
header{border-bottom:1px solid var(--line);margin-bottom:18px;padding-bottom:14px}
header h1{margin:0;font-size:20px}header .meta{color:var(--muted);font-size:13px;margin-top:4px}
.msg{border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:14px 0}
.msg.user{background:#20232a}.msg.hexiel{background:#1b1d23}
.head{display:flex;gap:10px;align-items:baseline;margin-bottom:6px}
.who{font-weight:700;color:var(--accent)}time{color:var(--muted);font-size:12px}
.body pre{background:#0f1115;border:1px solid var(--line);border-radius:8px;padding:10px;overflow-x:auto}
.body code{font:13px/1.5 ui-monospace,Menlo,Consolas,monospace}
.body p,li{white-space:pre-wrap}h1,h2,h3,h4{margin:12px 0 6px}
.blockquote,.body blockquote{border-left:3px solid var(--accent);margin:8px 0;padding:2px 12px;color:#c3c9d6}
details.tool{background:#14161b;border:1px solid var(--line);border-radius:8px;padding:6px 10px;margin:8px 0}
details.tool summary{cursor:pointer;color:var(--muted);font-size:13px;font-family:ui-monospace,Menlo,Consolas,monospace}
details.tool pre{margin:8px 0 2px;color:#b9c0d0}
details.tool.err summary{color:#e0a0a0}
.divider{text-align:center;color:var(--muted);font-style:italic;margin:18px 0}
footer{color:var(--muted);font-size:12px;margin-top:28px;border-top:1px solid var(--line);padding-top:12px}
a{color:var(--accent)}
"""


def export_html(session, target: Path | None = None) -> Path:
    """Write the transcript next to the session's history file (default) and return it."""
    import datetime
    if target is None:
        target = home_data_dir() / "sessions" / f"{session.id}.html"
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        from . import redact
    except Exception:  # noqa: BLE001 - redaction failing must not kill a share
        redact = None
    scrub = getattr(redact, "scrub", None) or (lambda text: text)   # the same redactor the saved chat gets
    project = Path(session.cwd).name or session.cwd
    stamp = datetime.datetime.now().astimezone()
    from . import __version__
    title = f"{project} — hexiel transcript"
    body = transcript_html(session)
    # scrub last over the joined body: providers never see this file, but a copy
    # that leaves the machine shouldn't either
    body = scrub(body)
    html_doc = (
        "<!doctype html>\n<html><head><meta charset='utf-8'>\n"
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head>\n<body>\n"
        f"<header><h1>{html.escape(project)}</h1>"
        f"<div class='meta'>conversation shared by hexiel {__version__} · {stamp:%Y-%m-%d %H:%M} · "
        f"session <code>{session.id}</code> · cwd <code>{html.escape(session.cwd)}</code></div></header>\n"
        f"{body}\n"
        f"<footer>self-contained single file — no server, no tracking. Generated by hexiel {__version__}.</footer>\n"
        "</body></html>\n")
    target.write_text(html_doc)
    return target