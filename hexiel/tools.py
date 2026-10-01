"""The native tool set: files, images, web, testing, containers, skills, bash."""

import base64
import hashlib
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import httpx

from . import skills as skills_mod
from .tooling import Toolchain

MAX_OUTPUT = 30_000       # characters returned to the model from any tool
MAX_BINARY = 256 * 1024   # refuse to read binaries above this size


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict          # JSON schema
    readonly: bool = False    # readonly tools never need approval
    always_ok: bool = False   # housekeeping tools (todo bookkeeping) never need approval
    run: object = None        # callable(args: dict, ctx: ToolContext) -> ToolResult


@dataclass
class ToolResult:
    text: str
    images: list[dict] = field(default_factory=list)  # {media_type, data(b64), approx_tokens}
    is_error: bool = False


@dataclass
class ToolContext:
    cwd: str
    chain: Toolchain
    read_files: set = field(default_factory=set)   # tracks read-before-edit
    stop_event: object = None                      # threading.Event for cancellation
    search_backend: str = "duckduckgo"
    searxng_url: str = ""
    skills: list = field(default_factory=list)
    on_step: object = None   # approve gate for propose flows: (kind, description) -> bool
    result_cache: dict = field(default_factory=dict)   # dedup pure lookups within a session
    dir_cache: dict = field(default_factory=dict)      # path/depth -> (fingerprint, output)


# pure read tools whose identical calls can be answered from cache (token economy)
CACHEABLE_TOOLS = {"read", "grep", "glob", "list", "map", "file_info", "web_fetch", "view_image"}


def text_result(text: str, is_error: bool = False) -> ToolResult:
    return ToolResult(text=text[:MAX_OUTPUT], is_error=is_error)


def head_tail(text: str, head: int = 10_000, tail: int = 5_000) -> str:
    """Keep both ends of a long local output — errors surface at the top
    (head) and the verdict/summary lands at the bottom (tail); the middle
    of a wall of logs is rarely worth its tokens."""
    if len(text) <= head + tail:
        return text
    elided = len(text) - head - tail
    return (text[:head] + f"\n\n... [{elided} chars elided locally to save context] ...\n\n"
            + text[-tail:])


# ---------------------------------------------------------------- shells

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _clean(text: str) -> str:
    """Strip ANSI color escapes (pwsh colorizes PowerShell errors)."""
    return _ANSI.sub("", text)


def _run_shell(args: dict, ctx: ToolContext, argv: list[str], shell_label: str) -> ToolResult:
    """Shared shell runner (bash / powershell): same timeout, head+tail capping,
    and error shape for every platform."""
    cmd = args.get("command", "").strip()
    if not cmd:
        return text_result("error: command is required", True)
    timeout = float(args.get("timeout") or 120)
    try:
        proc = subprocess.run(argv, cwd=ctx.cwd,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return text_result(f"error: command timed out after {timeout}s", True)
    except FileNotFoundError:
        return text_result(f"error: {shell_label} not installed", True)
    except PermissionError:
        return text_result(f"error: {shell_label} is not executable", True)
    out = []
    if proc.stdout:
        out.append(head_tail(_clean(proc.stdout)))
    if proc.stderr.strip():
        out.append(f"stderr:\n{head_tail(_clean(proc.stderr.strip()), 4000, 2000)}")
    summary = "\n".join(out) or "(no output)"
    if proc.returncode != 0:
        summary += f"\n(exit code {proc.returncode})"
    return text_result(summary, is_error=proc.returncode != 0)


def _pwsh_argv() -> str | None:
    """Locate a PowerShell for the powershell tool (pwsh preferred, then the
    Windows built-in powershell.exe)."""
    for exe in ("pwsh", "pwsh.exe", "powershell.exe", "powershell"):
        path = shutil.which(exe)
        if path:
            return path
    return None


def _run_powershell(args: dict, ctx: ToolContext) -> ToolResult:
    pwsh = _pwsh_argv()
    if not pwsh:
        return text_result(
            "error: no PowerShell on this machine — install one with "
            "`winget install Microsoft.PowerShell` (Windows) or "
            "`sudo pacman -S powershell` (Arch)", True)
    argv = [pwsh, "-NoProfile", "-NonInteractive", "-Command", args.get("command", "")]
    return _run_shell(args, ctx, argv, "pwsh")


def _run_bash(args: dict, ctx: ToolContext) -> ToolResult:
    # `bash` is the agent's default shell everywhere; on Windows without a real
    # bash (WSL/git-bash are both on PATH when present) fall back to PowerShell
    if os.name == "nt" and not shutil.which("bash"):
        pwsh = _pwsh_argv()
        if pwsh:
            return _run_powershell(args, ctx)
        return text_result(
            "error: neither bash nor PowerShell is available — install PowerShell "
            "with `winget install Microsoft.PowerShell`", True)
    return _run_shell(args, ctx, ["/bin/bash", "-lc", args.get("command", "")], "/bin/bash")


def _python_argv(ctx: ToolContext) -> str:
    """The project's own venv python when there is one (its packages import),
    else the interpreter hexiel itself runs on."""
    for sub in (".venv", "venv"):
        for rel in ("bin/python", "Scripts/python.exe"):
            cand = Path(ctx.cwd) / sub / rel
            if cand.is_file():
                return str(cand)
    return sys.executable


def _run_python(args: dict, ctx: ToolContext) -> ToolResult:
    """Run a Python script locally. The cheap way for a model on slow hardware
    to do many steps at once: one script that reads/greps/parses/computes and
    prints only the result replaces a chain of tool calls, each of which would
    cost a full model round-trip."""
    import tempfile
    code = args.get("code", "")
    if not code.strip():
        return text_result("error: code is required", True)
    fd, script = tempfile.mkstemp(prefix="hexiel-py-", suffix=".py")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(code)
        return _run_shell({"command": "python", "timeout": args.get("timeout")}, ctx,
                          [_python_argv(ctx), script], "python")
    finally:
        try:
            os.unlink(script)
        except OSError:
            pass


# ---------------------------------------------------------------- read / write / edit

def _read(args: dict, ctx: ToolContext) -> ToolResult:
    path = _resolve_path(args["path"], ctx)
    if path.is_dir():
        return text_result("error: path is a directory; use `list`", True)
    if not path.exists():
        return text_result("error: file not found", True)
    if path.stat().st_size > MAX_BINARY:
        return text_result("error: file too large (>256KB); read it with bash head/tail ranges", True)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return text_result(f"error: {exc}", True)
    if b"\x00" in raw[:8000]:
        return text_result(f"binary file ({path.stat().st_size} bytes); "
                           "use bash with xxd/strings to inspect it", True)
    text = raw.decode("utf-8", errors="replace")
    offset = int(args.get("offset") or 0)
    limit = int(args.get("limit") or 2000)
    lines = text.splitlines()
    window = lines[offset:offset + limit]
    numbered = "\n".join(f"{n + offset + 1:>6}  {line}" for n, line in enumerate(window))
    tail_note = ""
    if offset + limit < len(lines):
        tail_note = f"\n... ({len(lines) - offset - limit} more lines; use offset/limit)"
    out = f"{path}\n{numbered}{tail_note}"
    if len(out) > MAX_OUTPUT:
        out = out[:MAX_OUTPUT]
    ctx.read_files.add(str(path))
    return text_result(out)


def _resolve_path(rel: str, ctx: ToolContext) -> Path:
    return Path(rel) if Path(rel).is_absolute() else Path(ctx.cwd) / rel


def _write(args: dict, ctx: ToolContext) -> ToolResult:
    path = _resolve_path(args["path"], ctx)
    content = args.get("content", "")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        path.write_text(content)
    except OSError as exc:
        return text_result(f"error: {exc}", True)
    ctx.read_files.add(str(path))
    return text_result(f"{'updated' if existed else 'created'} {path} ({content.count(chr(10)) + 1} lines)"
                       + syntax_check(path))


def syntax_check(path: Path) -> str:
    """Instant local syntax check after a write/edit; '' when fine or unknown.
    Catches broken edits in the same step instead of a later failed run."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".py":
            compile(path.read_text(), str(path), "exec")
        elif suffix == ".json":
            import json as _json
            _json.loads(path.read_text())
        elif suffix == ".toml":
            import tomllib
            tomllib.loads(path.read_text())
        elif suffix in (".sh", ".bash") and shutil.which("bash"):
            proc = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, timeout=10)
            if proc.returncode:
                return f"\n⚠ syntax check failed: {proc.stderr.strip()[:500]}"
        elif suffix in (".js", ".mjs", ".cjs") and shutil.which("node"):
            proc = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, timeout=15)
            if proc.returncode:
                return f"\n⚠ syntax check failed: {proc.stderr.strip()[-500:]}"
    except SyntaxError as exc:
        return f"\n⚠ syntax check failed: {exc.msg} (line {exc.lineno}) — fix it now"
    except ValueError as exc:            # JSON / TOML decode errors
        return f"\n⚠ syntax check failed: {exc} — fix it now"
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _edit(args: dict, ctx: ToolContext) -> ToolResult:
    path = _resolve_path(args["path"], ctx)
    if not path.exists():
        return text_result("error: file not found", True)
    old, new = args["old_string"], args.get("new_string", "")
    try:
        text = path.read_text()
    except OSError as exc:
        return text_result(f"error: {exc}", True)
    if old not in text:
        return text_result("error: old_string not found in file (exact match required)", True)
    count = text.count(old)
    if count > 1 and not bool(args.get("replace_all")):
        return text_result(
            f"error: old_string occurs {count} times; make it unique or pass replace_all=true", True)
    text = text.replace(old, new) if bool(args.get("replace_all")) else text.replace(old, new, 1)
    try:
        path.write_text(text)
    except OSError as exc:
        return text_result(f"error: {exc}", True)
    ctx.read_files.add(str(path))
    return text_result(f"edited {path}" + syntax_check(path))


# ---------------------------------------------------------------- glob / grep / list / file_info

def _glob(args: dict, ctx: ToolContext) -> ToolResult:
    pattern = args["pattern"]
    base = Path(args.get("path") or ctx.cwd)
    try:
        matches = sorted(str(p) for p in base.rglob(pattern) if p.is_file())[:200]
    except (OSError, IndexError) as exc:
        return text_result(f"error: {exc}", True)
    return text_result("\n".join(matches) or "(no matches)")


_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pixi",
              "vendor", "dist", "build", "target", ".mypy_cache", ".ruff_cache"}


def _grep(args: dict, ctx: ToolContext) -> ToolResult:
    pattern = args["pattern"]
    base = Path(args.get("path") or ctx.cwd)
    file_glob = args.get("glob", "")
    ignore_case = bool(args.get("ignore_case"))
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        return text_result(f"error: bad regex: {exc}", True)
    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "--no-heading", "-n", "-m", "80", "--color=never"]
        if ignore_case:
            cmd.append("-i")
        if file_glob:
            cmd += ["-g", file_glob]
        cmd += ["-e", pattern, str(base)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return text_result(proc.stdout.strip() or "(no matches)")
    hits = []
    try:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                if file_glob and not Path(name).match(file_glob):
                    continue
                p = Path(dirpath) / name
                try:
                    if p.stat().st_size > 1_000_000:
                        continue
                    for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                        if rx.search(line):
                            hits.append(f"{p}:{i}: {line.strip()[:400]}")
                            if len(hits) > 100:
                                break
                except OSError:
                    continue
                if len(hits) > 100:
                    break
            if len(hits) > 100:
                break
    except OSError as exc:
        return text_result(f"error: {exc}", True)
    return text_result("\n".join(hits) or "(no matches)")


def _list(args: dict, ctx: ToolContext) -> ToolResult:
    path = _resolve_path(args.get("path") or ".", ctx)
    if not path.is_dir():
        return text_result("error: not a directory", True)
    try:
        entries = sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name))[:500]
        lines = [f"{e.name}{'/' if e.is_dir() else ''}"
                 + ("" if e.is_dir() else f"  ({e.stat().st_size} B)") for e in entries]
    except OSError as exc:
        return text_result(f"error: {exc}", True)
    return text_result("\n".join(lines) or "(empty)")


def _tree_lines(path: Path, depth: int, budget: int) -> tuple[list[str], bool]:
    """Recursive compact tree: `dir/` then `size name` lines. Local work only."""
    lines: list[str] = []
    try:
        entries = sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError:
        return ["(unscannable)"], False
    for e in entries:
        if e.name in _SKIP_DIRS or e.name.startswith(".") or e.name == "__pycache__":
            continue
        try:
            if e.is_dir():
                if depth <= 0:
                    lines.append(f"{e.name}/")
                    continue
                lines.append(f"{e.name}/")
                if len(lines) > budget:
                    return lines, True
                sub, truncated = _tree_lines(e, depth - 1, budget - len(lines))
                lines.extend("  " + l for l in sub)
                if truncated:
                    return lines, True
            else:
                size = e.stat().st_size
                lines.append(f"{e.name:40} {size // 1024 or 1}k")
        except OSError:
            continue
        if len(lines) > budget:
            return lines, True
    return lines, False


def _fingerprint(path: Path) -> tuple:
    """Cheap change signal for a tree: top-two-level dir name+mtime tuples.
    Saves re-walking when nothing changed; miss still rescans only locally."""
    marks = []
    try:
        marks.append((str(path), path.stat().st_mtime_ns))
        for e in sorted(path.iterdir()):
            if e.is_dir():
                marks.append((e.name, e.stat().st_mtime_ns))
    except OSError:
        pass
    return tuple(marks)


def _map(args: dict, ctx: ToolContext) -> ToolResult:
    """Compact project tree with file sizes — orientation in ONE cheap call
    instead of chained list/glob/read probing. Cached until the tree changes."""
    base = _resolve_path(args.get("path") or ".", ctx)
    depth = min(int(args.get("depth") or 3), 6)
    if not base.is_dir():
        return text_result("error: not a directory", True)
    fp = _fingerprint(base)
    key = (str(base), depth)
    cached = ctx.dir_cache.get(key)
    if cached and cached[0] == fp:
        return text_result(cached[1] + "\n(cached — project map unchanged)")
    lines, truncated = _tree_lines(base, depth, budget=400)
    out = f"project map of {base} (depth {depth}{', truncated' if truncated else ''}):\n"
    out += "\n".join(lines)
    if truncated:
        out += "\n...(size budget reached; drill in with map path=... or list)"
    ctx.dir_cache[key] = (fp, out)
    return text_result(out)


def _file_info(args: dict, ctx: ToolContext) -> ToolResult:
    path = _resolve_path(args["path"], ctx)
    try:
        stat = path.stat()
    except OSError as exc:
        return text_result(f"error: {exc}", True)
    kind = "directory" if path.is_dir() else "file"
    info = [f"{path}", f"  type: {kind}", f"  size: {stat.st_size} bytes",
            f"  modified: {stat.st_mtime:.0f}", f"  permissions: {oct(stat.st_mode)[-4:]}"]
    if path.is_file() and stat.st_size < MAX_BINARY:
        try:
            raw = path.read_bytes()
            if b"\x00" not in raw[:8000]:
                text = raw.decode("utf-8", errors="replace")
                info.append(f"  lines: {text.count(chr(10)) + 1}")
                info.append("  head:\n" + "\n".join("    " + l for l in text.splitlines()[:5]))
        except OSError:
            pass
    return text_result("\n".join(info))


# ---------------------------------------------------------------- images

def parse_image_size(data: bytes) -> tuple[int, int, str] | None:
    """Pure-python dimension probe for PNG, JPEG, GIF, WEBP (no PIL dependency)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"), "png")
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return (int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little"), "gif")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        try:
            return (int.from_bytes(data[26:30], "little"), int.from_bytes(data[24:26], "little"), "webp")
        except (struct.error, IndexError):
            return None
    if data[:2] == b"\xff\xd8":  # JPEG: walk SOF markers
        i = 2
        try:
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return (w, h, "jpeg")
                seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
                i += 2 + seg_len
        except (struct.error, IndexError):
            return None
    return None


def image_payload(data: bytes) -> tuple[dict, str] | None:
    """Encode image bytes as a conversation attachment; None if not png/jpeg/gif/webp."""
    probe = parse_image_size(data)
    if not probe:
        return None
    w, h, fmt = probe
    tokens = max(w * h // 750, 100)
    return ({"media_type": f"image/{fmt}", "data": base64.b64encode(data).decode(),
             "approx_tokens": tokens}, f"{w}x{h} {fmt}, ~{tokens} vision tokens")


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
_PATH_TOKEN = re.compile(r"""@?(?:"([^"]+)"|'([^']+)'|(\S+))""")
MAX_ATTACH_BYTES = 20 * 1024 * 1024


def attach_images(text: str, cwd: str) -> tuple[list[dict], list[str]]:
    """Find image paths typed in a user message (`@shot.png`, `~/Pictures/a.jpg`,
    quoted paths with spaces) and load them locally — the model sees the image
    directly instead of spending a tool round-trip on view_image.
    Returns (images, notes) where notes describe each attachment."""
    images, notes, seen = [], [], set()
    for m in _PATH_TOKEN.finditer(text):
        raw = (m.group(1) or m.group(2) or m.group(3) or "").rstrip(",.;:!?)")
        if not raw.lower().endswith(IMAGE_EXTS):
            continue
        path = Path(os.path.expanduser(raw))
        path = path if path.is_absolute() else Path(cwd) / path
        try:
            if path in seen or not path.is_file() or path.stat().st_size > MAX_ATTACH_BYTES:
                continue
            payload = image_payload(path.read_bytes())
        except OSError:
            continue
        if payload:
            seen.add(path)
            images.append(payload[0])
            notes.append(f"(attached image: {path} — {payload[1]})")
    return images, notes


def _view_image(args: dict, ctx: ToolContext) -> ToolResult:
    path = _resolve_path(args["path"], ctx)
    try:
        data = path.read_bytes()
    except OSError as exc:
        return text_result(f"error: {exc}", True)
    probe = parse_image_size(data)
    lines = [f"{path}", f"  format: {probe[2] if probe else 'unknown'}"]
    payload = image_payload(data)
    if payload:
        lines.append(f"  {payload[1]}")
        return ToolResult(text="\n".join(lines), images=[payload[0]])
    return ToolResult(text="\n".join(lines) +
                      "\n  (unsupported format; only png/jpeg/gif/webp attach to the conversation)",
                      is_error=True)


def _image_edit(args: dict, ctx: ToolContext) -> ToolResult:
    action = args.get("action", "resize")
    magick = shutil.which("magick") or shutil.which("convert")
    if not magick:
        return text_result("error: ImageMagick not installed (e.g. `sudo pacman -S imagemagick`)", True)
    path = _resolve_path(args["path"], ctx)
    out = _resolve_path(args["out"], ctx) if args.get("out") else path
    if action == "convert" and not args.get("out"):
        return text_result("error: convert needs an `out` path", True)
    cmd = [magick, str(path)]
    if action == "resize":
        cmd += ["-resize", args.get("size", "50%")]
    elif action == "rotate":
        cmd += ["-rotate", str(args.get("degrees", 90))]
    elif action != "convert":
        return text_result("error: action must be resize|rotate|convert", True)
    cmd += [str(out)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=ctx.cwd, timeout=120)
    except subprocess.TimeoutExpired:
        return text_result("error: magick timed out", True)
    if proc.returncode != 0:
        return text_result(f"error: magick failed: {proc.stderr[:300]}", True)
    dims = ""
    try:
        probe = parse_image_size(out.read_bytes())
        if probe:
            dims = f" ({probe[2]}, {probe[0]}x{probe[1]})"
    except OSError:
        pass
    return text_result(f"image written to {out}{dims}")


# ---------------------------------------------------------------- web

class _TextExtract(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs) -> None:
        # nav/header/footer boilerplate is stripped locally so the model pays
        # tokens for content only
        if tag in ("script", "style", "noscript", "template", "nav",
                   "header", "footer", "aside", "form", "svg"):
            self._skip += 1

    def handle_endtag(self, tag) -> None:
        if self._skip and tag in ("script", "style", "noscript", "template", "nav",
                                  "header", "footer", "aside", "form", "svg"):
            self._skip -= 1

    def handle_data(self, data) -> None:
        if not self._skip and data.strip():
            self.chunks.append(data.strip())


def html_to_text(html: str) -> str:
    parser = _TextExtract()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - malformed html must not kill the tool
        pass
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(parser.chunks))
    # collapse run-together headings/menu repeats that survive tag stripping
    seen_prev = None
    deduped = []
    for line in text.splitlines():
        if line != seen_prev:
            deduped.append(line)
        seen_prev = line
    return "\n".join(deduped)


def _fetch_cache_path(url: str, ctx: ToolContext) -> Path:
    digest = hashlib.sha1(url.encode()).hexdigest()[:16]
    return Path(ctx.cwd) / ".hexiel" / "webcache" / f"{digest}.txt"


def _web_fetch(args: dict, ctx: ToolContext) -> ToolResult:
    url = args["url"]
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            resp = client.get(url, headers={"user-agent": "hexiel-agent/0.1 (+local)"})
    except httpx.HTTPError as exc:
        return text_result(f"error: fetch failed: {exc}", True)
    ctype = resp.headers.get("content-type", "")
    body = html_to_text(resp.text) if "html" in ctype else resp.text
    limit = int(args.get("max_chars") or 16000)
    # full distilled text on disk: `read offset/limit` it if more is needed,
    # without another fetch (and without another web round-trip)
    cache_path = _fetch_cache_path(url, ctx)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(body)
    except OSError:
        cache_path = None
    head = f"{resp.url} [{resp.status_code}] {ctype}\n\n"
    head += body[:limit + 80]
    if len(body) > limit:
        more = (f"\n\n... (truncated at {limit}; {len(body)} chars total — "
                f"`read {cache_path}` with offset/limit for the rest)"
                if cache_path else "\n\n... (truncated)")
        head += more
    return text_result(head, is_error=resp.status_code >= 400)


def search_results(query: str, limit: int, ctx: ToolContext) -> list[dict]:
    """[{title, url, snippet}] from the configured backend (raises on failure)."""
    if ctx.search_backend.startswith("searx"):
        if not ctx.searxng_url:
            raise RuntimeError("search_backend=searxng but searxng_url is not set")
        base = ctx.searxng_url.rstrip("/")
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            resp = client.get(f"{base}/search", params={"q": query, "format": "json"},
                              headers={"user-agent": "hexiel-agent/0.1"})
            resp.raise_for_status()
            data = resp.json()
        return [{"title": r.get("title", ""), "url": r.get("url", ""),
                 "snippet": r.get("content", "")} for r in data.get("results", [])[:limit]]
    return _parse_ddg(_duckduckgo_html(query), limit)


def fetch_text(url: str, ctx: ToolContext, timeout: float = 20) -> str:
    """Distilled page text (also parked in the web cache). Raises on failure."""
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url, headers={"user-agent": "hexiel-agent/0.1 (+local)"})
    resp.raise_for_status()
    body = html_to_text(resp.text) if "html" in resp.headers.get("content-type", "") else resp.text
    try:
        path = _fetch_cache_path(url, ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    except OSError:
        pass
    return body


def _web_search(args: dict, ctx: ToolContext) -> ToolResult:
    query = args["query"]
    limit = int(args.get("max_results") or 5)
    try:
        results = search_results(query, limit, ctx)
    except Exception as exc:  # noqa: BLE001
        return text_result(f"error: search failed: {exc}", True)
    if not results:
        return text_result("(no results)")
    lines = [f"{i + 1}. {r['title']}\n   {r['url']}\n   {r['snippet'][:200]}"
             for i, r in enumerate(results)]
    return text_result("\n".join(lines) + "\n\nUse web_fetch on a result's URL to read it.")


def _duckduckgo_html(query: str) -> str:
    for engine in ("https://html.duckduckgo.com/html/?q=", "https://duckduckgo.com/html/?q="):
        try:
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                resp = client.get(engine + query.replace(" ", "+"),
                                  headers={"user-agent": "Mozilla/5.0 (X11; hexiel-agent)"})
                resp.raise_for_status()
                return resp.text
        except httpx.HTTPError:
            continue
    raise RuntimeError("duckduckgo unreachable")


def _parse_ddg(html: str, limit: int) -> list[dict]:
    from urllib.parse import parse_qs, unquote, urlparse
    results = []
    for link in re.finditer(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                            html, re.DOTALL):
        raw_url, title = link.groups()
        title = re.sub(r"<[^>]+>", "", title).strip()
        url = raw_url
        if "uddg=" in url:
            url = unquote(parse_qs(urlparse(url).query).get("uddg", [url])[0])
        if url.startswith("//"):
            url = "https:" + url
        results.append({"title": title, "url": url, "snippet": ""})
        if len(results) >= limit:
            break
    return results


# ---------------------------------------------------------------- test / container

def _test(args: dict, ctx: ToolContext) -> ToolResult:
    if not ctx.chain.test_runners:
        return text_result("error: no test runner detected in this project; use bash directly", True)
    label = args.get("runner") or ctx.chain.test_runners[0].label
    runner = next((r for r in ctx.chain.test_runners if r.label == label), None)
    if runner is None:
        labels = ", ".join(r.label for r in ctx.chain.test_runners)
        return text_result(f"error: unknown runner '{label}' (detected: {labels})", True)
    try:
        proc = subprocess.run(runner.command, cwd=ctx.cwd, capture_output=True, text=True,
                              timeout=float(args.get("timeout") or 900))
    except subprocess.TimeoutExpired:
        return text_result(f"error: {runner.label} timed out", True)
    combined = (proc.stdout + "\n" + proc.stderr).strip()
    if proc.returncode == 0:
        tail = "\n".join(combined.splitlines()[-25:])
        return text_result(f"tests passed ({runner.label})\n\n{tail}")
    failing = [ln for ln in combined.splitlines()
               if re.search(r"FAILED|ERROR|Failing|Failures?|failed", ln, re.I)]
    picked = failing[:25] or combined.splitlines()[-40:]
    return text_result(f"tests FAILED ({runner.label}, exit {proc.returncode})\n\n"
                       + "\n".join(picked), is_error=True)


def _container(args: dict, ctx: ToolContext) -> ToolResult:
    runtime = ctx.chain.container
    if not runtime:
        return text_result("error: no container runtime detected (neither docker nor podman)", True)
    action = args.get("action", "list")
    bin_ = runtime.binary
    cmd: list[str] = []
    if action in ("list", "ps"):
        cmd = [bin_, "ps", "-a", "--format", "{{.Names}}  {{.Status}}  {{.Image}}"]
    elif action == "images":
        cmd = [bin_, "images", "--format", "{{.Repository}}:{{.Tag}}  {{.Size}}"]
    elif action == "run":
        if not args.get("image"):
            return text_result("error: run needs `image` (use action=list to see what's running)", True)
        cmd = [bin_, "run", "-d"]
        for port in args.get("ports") or []:
            cmd += ["-p", str(port)]
        for vol in args.get("volumes") or []:
            cmd += ["-v", str(vol)]
        if args.get("name"):
            cmd += ["--name", args["name"]]
        cmd += [args["image"]]
        if args.get("cmd"):
            cmd += _cmd_list(args["cmd"])
    elif action == "exec":
        if not args.get("container") or not args.get("cmd"):
            return text_result("error: exec needs `container` and `cmd`", True)
        cmd = [bin_, "exec", args["container"]] + _cmd_list(args["cmd"])
    elif action == "logs":
        if not args.get("container"):
            return text_result("error: logs needs `container`", True)
        cmd = [bin_, "logs", "--tail", str(args.get("tail") or 100), args["container"]]
    elif action == "build":
        if not args.get("path"):
            return text_result("error: build needs `path` (the dockerfile's build context)", True)
        cmd = [bin_, "build", "-t", args.get("tag", "hexiel-build"), args["path"]]
    elif action == "compose":
        if not runtime.compose_command:
            return text_result(f"error: {bin_} compose is not available on this machine", True)
        sub = args.get("compose_action", "up")
        tails = {"up": ["up", "-d"], "down": ["down"], "logs": ["logs", "--tail", "100"],
                 "build": ["build"], "ps": ["ps"]}
        if sub not in tails:
            return text_result("error: compose_action must be up|down|logs|build|ps", True)
        cmd = list(runtime.compose_command) + tails[sub]
        if sub == "up" and args.get("path"):
            cmd += ["-f", str(_resolve_path(args["path"], ctx))]
    elif action in ("stop", "rm"):
        if not args.get("container"):
            return text_result(f"error: {action} needs `container`", True)
        cmd = [bin_, action, args["container"]]
    else:
        return text_result(
            "error: action must be list|run|exec|logs|build|compose|stop|rm|images", True)

    try:
        proc = subprocess.run(cmd, cwd=ctx.cwd, capture_output=True, text=True,
                              timeout=float(args.get("timeout") or 120))
    except subprocess.TimeoutExpired:
        return text_result(f"error: container command timed out: {shlex.join(cmd)}", True)
    out = proc.stdout + ("\n" + proc.stderr if proc.stderr.strip() else "")
    summary = head_tail(out.strip()) or "(no output)"
    if proc.returncode != 0:
        summary += f"\n(exit code {proc.returncode})"
    return text_result(f"$ {shlex.join(cmd)}\n{summary}", is_error=proc.returncode != 0)


def _cmd_list(user_cmd) -> list[str]:
    if isinstance(user_cmd, str):
        return shlex.split(user_cmd)
    return [str(part) for part in user_cmd]


# ---------------------------------------------------------------- skill

def _skill(args: dict, ctx: ToolContext) -> ToolResult:
    name = args["name"]
    skill = skills_mod.find(ctx.skills, name)
    if skill is None:
        names = ", ".join(s.name for s in ctx.skills) or "(none installed)"
        return text_result(f"error: unknown skill '{name}'. available: {names}", True)
    loaded = (f"# Loaded skill: {skill.name}\nSource: {skill.path}\n\n{skill.body}\n\n---\n"
              "These instructions are now active. Follow them for the current task, in place "
              "of your default approach.")
    return text_result(loaded)


def _todo(args: dict, ctx: ToolContext) -> ToolResult:
    from . import todo as todo_mod
    action = args.get("action", "list")
    fn = todo_mod.ACTIONS.get(action)
    if fn is None:
        return text_result(f"error: action must be one of {', '.join(todo_mod.ACTIONS)}", True)
    return text_result(fn(args, ctx.cwd))


def _gitlab_mr(args: dict, ctx: ToolContext) -> ToolResult:
    from . import gitlab as gitlab_mod
    action = args.get("action", "status")
    try:
        if action == "status":
            mr = gitlab_mod.mr_status(ctx.cwd, int(args["mr_iid"]))
            return text_result(json.dumps(mr, indent=2))
        if action == "propose":
            result = gitlab_mod.propose_change(
                ctx.cwd, args["title"], args.get("body", ""),
                branch=args.get("branch"), files=args.get("files"),
                on_step=ctx.on_step)
            url = result.get("mr_url", "")
            return text_result(("merge request created: " + url) if url
                               else f"branch {result['branch']} pushed but MR not created: "
                                    "/setup-gitlab for the API token")
        if action == "propose_from_fork":
            import urllib.parse
            host = args.get("host", "gitlab.com")
            from .gitlab import GitlabProject, _parse_remote
            origin = gitlab_mod._git(ctx.cwd, "remote", "get-url", "origin")
            # explicit upstream in args; if none, fall back to origin detection
            if args.get("upstream_path"):
                encoded = urllib.parse.quote(args["upstream_path"], safe="")
            elif origin:
                host, path = _parse_remote(origin)
                encoded = urllib.parse.quote(path, safe="")
            else:
                return text_result("error: supply upstream_path or a git remote origin", True)
            project = GitlabProject(host=host, path=encoded,
                                    api_base=f"https://{host}/api/v4",
                                    token=gitlab_mod.load_token())
            result = gitlab_mod.propose_from_fork(
                ctx.cwd, project, args["title"], args.get("body", ""),
                on_step=ctx.on_step)
            return text_result(f"merge request from fork created: {result.get('mr_url', '')}")
    except gitlab_mod.GitlabError as exc:
        return text_result(f"error: {exc}", True)
    return text_result("error: action must be status|propose|propose_from_fork", True)


# ---------------------------------------------------------------- registry

def build_tools(chain: Toolchain) -> list[ToolDef]:
    obj = lambda **props: {"type": "object", "properties": props}  # noqa: E731
    return [
        ToolDef("bash", "Run a shell command (/bin/bash -lc) in the project directory. "
                "Use for git, builds, installs, anything without a dedicated tool. "
                "On Windows this falls back to PowerShell when bash is absent.",
                obj(command={"type": "string"}, timeout={"type": "number"}),
                run=_run_bash),
        ToolDef("python", "Run a Python 3 script in the project directory (project venv if "
                "present) and get its stdout/stderr. Prefer ONE script over many tool "
                "calls: read several files, search, parse logs/JSON/XML, count, diff, "
                "transform, compute — then print only the lines you actually need.",
                obj(code={"type": "string", "description": "complete Python script"},
                    timeout={"type": "number"}),
                run=_run_python),
        ToolDef("powershell", "Run a PowerShell command (pwsh) in the project directory — "
                "Windows-native paths, registry, services, WMI, etc. Use instead of "
                "bash for Windows-specific operations.",
                obj(command={"type": "string"}, timeout={"type": "number"}),
                run=_run_powershell),
        ToolDef("read", "Read a text file with line numbers; supports offset/limit for long files.",
                obj(path={"type": "string"}, offset={"type": "integer"}, limit={"type": "integer"}),
                readonly=True, run=_read),
        ToolDef("write", "Create or overwrite a file with content.",
                obj(path={"type": "string"}, content={"type": "string"}),
                run=_write),
        ToolDef("edit", "Replace an exact string in a file. Read the file first; "
                "old_string must match exactly, and be unique unless replace_all=true.",
                obj(path={"type": "string"}, old_string={"type": "string"},
                    new_string={"type": "string"}, replace_all={"type": "boolean"}),
                run=_edit),
        ToolDef("glob", "Find files by glob pattern (e.g. **/*.py) under a path.",
                obj(pattern={"type": "string"}, path={"type": "string"}),
                readonly=True, run=_glob),
        ToolDef("grep", "Regex search file contents (uses ripgrep when available).",
                obj(pattern={"type": "string"}, path={"type": "string"},
                    glob={"type": "string"}, ignore_case={"type": "boolean"}),
                readonly=True, run=_grep),
        ToolDef("list", "List a directory's entries with sizes.",
                obj(path={"type": "string"}), readonly=True, run=_list),
        ToolDef("map", "Compact project tree (dirs + file sizes) for orientation — "
                "one cheap local call instead of chained list/glob probing. Cached.",
                obj(path={"type": "string"}, depth={"type": "integer"}),
                readonly=True, run=_map),
        ToolDef("file_info", "Stat a file: size, mtime, permissions, line count, first lines.",
                obj(path={"type": "string"}), readonly=True, run=_file_info),
        ToolDef("view_image", "Attach a local image (png/jpeg/gif/webp) to the conversation "
                "so a vision-capable model can see it.",
                obj(path={"type": "string"}), readonly=True, run=_view_image),
        ToolDef("image_edit", "Resize / rotate / convert an image via ImageMagick.",
                obj(path={"type": "string"},
                    action={"type": "string", "enum": ["resize", "rotate", "convert"]},
                    size={"type": "string"}, degrees={"type": "number"},
                    out={"type": "string"}),
                run=_image_edit),
        ToolDef("web_fetch", "Fetch a URL and return its text content (HTML stripped).",
                obj(url={"type": "string"}, max_chars={"type": "integer"}),
                readonly=True, run=_web_fetch),
        ToolDef("web_search", "Search the web (DuckDuckGo HTML or a configured SearXNG instance).",
                obj(query={"type": "string"}, max_results={"type": "integer"}),
                readonly=True, run=_web_search),
        ToolDef("test", "Run this project's natively-detected test runner (pytest, vitest/jest, "
                "go test, cargo test, make test). Pass `runner` only to pick among several.",
                obj(runner={"type": "string"}, timeout={"type": "number"}),
                run=_test),
        ToolDef("container", "Drive the detected container runtime (docker or podman) natively. "
                "actions: list, run (image, name, ports[], volumes[], cmd), exec (container, cmd), "
                "logs (container, tail), build (path, tag), compose (compose_action: up|down|logs|build|ps, path), "
                "stop, rm, images.",
                obj(action={"type": "string"}, image={"type": "string"}, name={"type": "string"},
                    container={"type": "string"}, cmd={"type": "string"},
                    path={"type": "string"}, tag={"type": "string"},
                    ports={"type": "array", "items": {"type": "string"}},
                    volumes={"type": "array", "items": {"type": "string"}},
                    tail={"type": "integer"},
                    compose_action={"type": "string"}, timeout={"type": "number"}),
                run=_container),
        ToolDef("skill", "Load an installed skill's instructions into context so its methodology "
                "replaces the default approach for the current task.",
                obj(name={"type": "string"}), readonly=True, run=_skill),
        ToolDef("todo", "Maintain the project's .hexiel/todo.md so multi-step work survives "
                "any interruption. Actions: create (title, steps[]), add (step, after?), "
                "update (step, state: ' '~' '), left_off (note), list.",
                obj(action={"type": "string", "enum": ["create", "add", "update",
                                                       "left_off", "list"]},
                    title={"type": "string"}, steps={"type": "array", "items": {"type": "string"}},
                    step={"type": "string"}, state={"type": "string"},
                    after={"type": "string"}, note={"type": "string"}),
                always_ok=True, run=_todo),
        ToolDef("gitlab_mr", "GitLab collaboration: propose (branch+commit+push+merge "
                "request to the project's origin), propose_from_fork (contributor flow: "
                "fork upstream, push your branch, open the MR for review), or "
                "status (mr_iid). The user approves each step via the approval gate.",
                obj(action={"type": "string", "enum": ["propose", "propose_from_fork", "status"]},
                    title={"type": "string"}, body={"type": "string"},
                    branch={"type": "string"}, mr_iid={"type": "integer"},
                    upstream_path={"type": "string"}, host={"type": "string"}),
                run=_gitlab_mr),
    ]