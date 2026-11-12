"""The skills & plugins marketplace: add catalogues (sources), search them, install / update / remove what they list.

A source is a JSON index at an https URL, a local file or folder, or a git repository. Two index shapes are read:

  hexiel's own   {"name": "...", "items": [{"name", "kind", "version", "description", "url", "sha256", "mcp", "tags"}]}
                 kind = skill (a SKILL.md, or a .zip / .tar.gz of a skill folder), plugin (a hexiel/Open WebUI .py
                 plugin), mcp (an .mcp.json-style server entry in "mcp"), bundle (a folder like a Claude Code plugin)
  Claude Code's  .claude-plugin/marketplace.json  {"name", "plugins": [{"name", "source", "description", "version"}]}
                 each plugin folder's skills/*/SKILL.md, commands/*.md and .mcp.json are installed

Integrity: an item with a sha256 is refused when the download does not match. What was installed (files, MCP
entries, the hash) is recorded, so `update` only reinstalls what changed and `remove` takes away exactly what came in.
A listing whose file changed but whose version did not is flagged, not silently installed. Plugins and MCP servers run
code on this machine, so installing one always says so; review the source first.

  hexiel marketplace add URL [NAME] | sources | search [WORDS] | info NAME | install NAME | update [NAME] | remove NAME
  /marketplace … in the terminal and web UI."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess  # nosec B404 - git clone of a source the user added
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import home_config_dir, home_data_dir

KINDS = ("skill", "plugin", "mcp", "bundle")
FETCH_TIMEOUT = 30
MAX_DOWNLOAD = 50 * 1024 * 1024


@dataclass
class Item:
    name: str
    kind: str
    source: str                      # the catalogue's name
    version: str = ""
    description: str = ""
    url: str = ""                    # file / archive / folder (resolved against the index's location)
    sha256: str = ""
    mcp: dict = field(default_factory=dict)
    tags: list = field(default_factory=list)


class MarketError(Exception):
    pass


def _root() -> Path:
    return home_data_dir() / "marketplace"


def _load(name: str, default):
    try:
        return json.loads((_root() / name).read_text())
    except (OSError, ValueError):
        return default


def _save(name: str, data) -> None:
    _root().mkdir(parents=True, exist_ok=True)
    tmp = _root() / f".{name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(_root() / name)


BUILTIN = {"name": "hexiel", "url": str(Path(__file__).resolve().parent / "starter"), "builtin": True}


def user_sources() -> list[dict]:
    return [s for s in _load("sources.json", []) if s.get("name") != BUILTIN["name"]]


def sources() -> list[dict]:
    """hexiel's own starter catalogue (shipped inside the package, works offline) plus the ones you added."""
    return [dict(BUILTIN)] + user_sources()


def installed() -> dict:
    return _load("installed.json", {})


# ---------------------------------------------------------------- fetching

def _is_git(url: str) -> bool:
    return url.endswith(".git") or url.startswith(("git@", "git+")) or bool(
        re.match(r"https://(github\.com|gitlab\.com|codeberg\.org)/[^/]+/[^/]+/?$", url))


def _fetch_bytes(url: str) -> bytes:
    if url.startswith(("http://", "https://")):
        import httpx
        with httpx.stream("GET", url, timeout=FETCH_TIMEOUT, follow_redirects=True) as r:
            r.raise_for_status()
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > MAX_DOWNLOAD:
                    raise MarketError(f"{url}: larger than {MAX_DOWNLOAD // 2**20} MB")
            return bytes(buf)
    path = Path(url.removeprefix("file://")).expanduser()
    try:
        return path.read_bytes()
    except OSError as exc:
        raise MarketError(f"cannot read {url}: {exc.strerror or exc}") from None


def _git_checkout(url: str) -> Path:
    """A shallow clone of a git source, cached and refreshed (fast-forward) on each fetch."""
    url = url.removeprefix("git+")
    dest = _root() / "git" / hashlib.sha256(url.encode()).hexdigest()[:16]
    try:
        if (dest / ".git").is_dir():
            subprocess.run(["git", "-C", str(dest), "pull", "--ff-only", "-q"], check=False,  # nosec B603 B607
                           capture_output=True, timeout=120)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "--depth", "1", "-q", url, str(dest)], check=True,  # nosec B603 B607
                           capture_output=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MarketError(f"git clone {url} failed: {getattr(exc, 'stderr', b'') or exc}") from None
    return dest


def _index_location(url: str) -> tuple[str, bytes]:
    """(base for relative links, index bytes) for any kind of source."""
    if _is_git(url):
        url = str(_git_checkout(url))
    if not url.startswith(("http://", "https://")):
        path = Path(url.removeprefix("file://")).expanduser()
        if path.is_dir():
            for name in ("hexiel-marketplace.json", "marketplace.json", ".claude-plugin/marketplace.json"):
                if (path / name).is_file():
                    return str(path), (path / name).read_bytes()
            raise MarketError(f"{path}: no marketplace.json or .claude-plugin/marketplace.json in it")
        return str(path.parent), _fetch_bytes(str(path))
    return url.rsplit("/", 1)[0], _fetch_bytes(url)


def _join(base: str, ref: str) -> str:
    if not ref or re.match(r"^[a-z]+://", ref) or ref.startswith("/"):
        return ref
    return f"{base.rstrip('/')}/{ref.removeprefix('./')}"


def parse_index(source: str, base: str, data: dict) -> list[Item]:
    items = []
    if isinstance(data.get("plugins"), list):              # Claude Code marketplace.json
        root = base
        meta = data.get("metadata") or {}
        if meta.get("pluginRoot"):
            root = _join(base, meta["pluginRoot"])
        for p in data["plugins"]:
            src = p.get("source")
            if isinstance(src, dict):                       # {"source": "github", "repo": "o/r"} / {"url": ...}
                src = f"https://github.com/{src['repo']}" if src.get("repo") else src.get("url", "")
            items.append(Item(name=p.get("name", ""), kind="bundle", source=source, version=str(p.get("version", "")),
                              description=p.get("description", ""), url=_join(root, str(src or "")),
                              tags=list(p.get("keywords") or p.get("tags") or [])))
    for it in data.get("items") or []:                      # hexiel's own shape
        kind = str(it.get("kind", "skill")).lower()
        if kind not in KINDS:
            continue
        items.append(Item(name=it.get("name", ""), kind=kind, source=source, version=str(it.get("version", "")),
                          description=it.get("description", ""), url=_join(base, it.get("url", "")),
                          sha256=str(it.get("sha256", "")).lower(), mcp=it.get("mcp") or {},
                          tags=list(it.get("tags") or [])))
    return [i for i in items if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", i.name or "")]


def catalogue(refresh: bool = False) -> list[Item]:
    """Every item from every source. Indexes are cached for an hour (refresh=True fetches now)."""
    cache = _load("cache.json", {})
    items, changed = [], False
    for src in sources():
        hit = cache.get(src["url"])
        if refresh or not hit or time.time() - hit.get("at", 0) > 3600:
            try:
                base, raw = _index_location(src["url"])
                hit = {"at": time.time(), "base": base, "data": json.loads(raw)}
                cache[src["url"]], changed = hit, True
            except (MarketError, ValueError, OSError) as exc:
                if not hit:
                    items.append(Item(name="", kind="error", source=src["name"], description=str(exc)))
                    continue
            except Exception as exc:  # noqa: BLE001 - one bad source must not hide the others
                if not hit:
                    items.append(Item(name="", kind="error", source=src["name"], description=str(exc)))
                    continue
        items.extend(parse_index(src["name"], hit["base"], hit["data"]))
    if changed:
        _save("cache.json", cache)
    return items


# ---------------------------------------------------------------- sources

def add_source(url: str, name: str = "") -> str:
    url = url.strip()
    base, raw = _index_location(url)                         # validate before remembering
    data = json.loads(raw)
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name or data.get("name") or Path(url.rstrip("/")).stem)[:40] or "source"
    count = len(parse_index(name, base, data))
    if name == BUILTIN["name"]:
        raise MarketError("'hexiel' is the built-in catalogue's name — pick another name for your source")
    srcs = [s for s in user_sources() if s["url"] != url and s["name"] != name]
    srcs.append({"name": name, "url": url})
    _save("sources.json", srcs)
    cache = _load("cache.json", {})
    cache[url] = {"at": time.time(), "base": base, "data": data}
    _save("cache.json", cache)
    return f"added source {name} ({count} item(s)) — /marketplace search to browse"


def remove_source(name: str) -> str:
    if name == BUILTIN["name"]:
        return "the built-in 'hexiel' catalogue ships with hexiel and cannot be removed"
    srcs = user_sources()
    keep = [s for s in srcs if s["name"] != name]
    if len(keep) == len(srcs):
        return f"no source called {name}"
    _save("sources.json", keep)
    return f"removed source {name} (installed items stay; /marketplace remove NAME to uninstall)"


# ---------------------------------------------------------------- search

def _score(item: Item, words: list[str]) -> float:
    if not words:
        return 1.0
    hay = f"{item.name} {item.description} {' '.join(item.tags)} {item.kind}".lower()
    score = 0.0
    for w in words:
        if w in item.name.lower():
            score += 3
        elif w in hay:
            score += 1
        else:
            from difflib import get_close_matches
            if get_close_matches(w, re.findall(r"[a-z0-9]+", hay), n=1, cutoff=0.8):
                score += 0.5
    return score


def search(query: str = "", kind: str = "") -> list[Item]:
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1]
    found = [(s, i) for i in catalogue() if i.kind != "error" and (not kind or i.kind == kind)
             for s in [_score(i, words)] if s > 0]
    found.sort(key=lambda p: (-p[0], p[1].name))
    return [i for _, i in found]


def find(name: str) -> Item:
    items = [i for i in catalogue() if i.name == name or f"{i.source}/{i.name}" == name]
    if not items:
        close = [i.name for i in search(name)[:3]]
        raise MarketError(f"nothing called {name!r} in the marketplace" + (f" — did you mean {', '.join(close)}?"
                                                                             if close else ""))
    return items[0]


def _vkey(v: str) -> tuple:
    return tuple(int(p) if p.isdigit() else -1 for p in re.split(r"[.+-]", v or "0")[:4])


def render(items: list[Item]) -> str:
    if not items:
        return "nothing found" if sources() else ("no marketplace sources yet — add one: /marketplace add URL "
                                                  "(an index .json, a folder, or a git repo with a marketplace.json)")
    have = installed()
    out = []
    for i in items:
        if i.kind == "error":
            out.append(f"  ✗ source {i.source}: {i.description}")
            continue
        mark = "●" if i.name in have else "○"
        upd = " ↑ update" if i.name in have and _vkey(i.version) > _vkey(have[i.name].get("version", "")) else ""
        out.append(f"  {mark} {i.name:<22} {i.kind:<7} {i.version or '-':<8} {i.description[:70]}{upd}")
    return "\n".join(out)


# ---------------------------------------------------------------- install

def _skills_dir() -> Path:
    return home_data_dir() / "skills"


def _plugins_dir() -> Path:
    return home_data_dir() / "plugins"


def _check(item: Item, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    if item.sha256 and digest != item.sha256:
        raise MarketError(f"{item.name}: the download does not match the sha256 in the index "
                          f"(expected {item.sha256[:12]}…, got {digest[:12]}…) — not installed")
    return digest


def _safe_extract(data: bytes, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()

    def inside(name: str) -> Path:
        target = (dest / name).resolve()
        if root != target and root not in target.parents:
            raise MarketError(f"archive entry escapes the install folder: {name}")
        return target
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                target = inside(info.filename)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(z.read(info))
        return
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            for member in t.getmembers():
                if not (member.isfile() or member.isdir()):
                    continue                                   # no links, devices or fifos
                target = inside(member.name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(t.extractfile(member).read())
    except tarfile.TarError:
        raise MarketError("not a zip or tar archive") from None


def _hash_tree(path: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in path.rglob("*") if p.is_file() and ".git" not in p.parts):
        h.update(str(f.relative_to(path)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()


def _bundle_folder(item: Item) -> Path:
    url = item.url
    if _is_git(url):
        return _git_checkout(url)
    if url.startswith(("http://", "https://")):
        data = _fetch_bytes(url)
        _check(item, data)
        tmp = Path(tempfile.mkdtemp(prefix="hexiel-market-"))
        _safe_extract(data, tmp)
        kids = [p for p in tmp.iterdir()]
        return kids[0] if len(kids) == 1 and kids[0].is_dir() else tmp
    path = Path(url.removeprefix("file://")).expanduser()
    if not path.is_dir():
        raise MarketError(f"{item.name}: {url} is not a folder")
    return path


def _mcp_file() -> Path:
    return home_config_dir() / "mcp.json"


def _add_mcp(entries: dict) -> list[str]:
    path = _mcp_file()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    data.setdefault("mcpServers", {}).update(entries)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    return list(entries)


def _drop_mcp(names: list[str]) -> None:
    path = _mcp_file()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    for n in names:
        data.get("mcpServers", {}).pop(n, None)
    path.write_text(json.dumps(data, indent=2))


def warning(item: Item) -> str:
    """What installing this item lets run on the machine — shown before every install."""
    if item.kind == "plugin":
        return "⚠ a plugin is Python code that runs inside hexiel with your permissions — review it first"
    if item.kind == "mcp":
        cmd = " ".join([item.mcp.get("command", "")] + list(item.mcp.get("args") or [])).strip()
        return f"⚠ an MCP server is a program hexiel starts: {cmd or item.mcp.get('url', '?')}"
    if item.kind == "bundle":
        return "⚠ a bundle can include MCP servers (programs hexiel starts) — review the source first"
    return "skills are instructions only (no code runs on install)"


def install(name: str, force: bool = False) -> str:
    item = find(name)
    have = installed()
    if name in have and not force:
        return f"{name} is already installed ({have[name].get('version') or 'no version'}) — /marketplace update {name}"
    if name in have:
        _uninstall_files(have[name])
    record = {"kind": item.kind, "version": item.version, "source": item.source, "url": item.url,
              "files": [], "mcp": [], "installed_at": time.strftime("%Y-%m-%d %H:%M")}
    if item.kind == "mcp":
        if not item.mcp:
            raise MarketError(f"{name}: the index gives no mcp entry")
        record["mcp"] = _add_mcp({name: item.mcp})
        record["sha256"] = hashlib.sha256(json.dumps(item.mcp, sort_keys=True).encode()).hexdigest()
    elif item.kind == "plugin":
        data = _fetch_bytes(item.url)
        record["sha256"] = _check(item, data)
        dest = _plugins_dir() / f"{name}.py"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        record["files"] = [str(dest)]
    elif item.kind == "skill":
        dest = _skills_dir() / name
        if item.url.endswith((".md", ".MD")):
            data = _fetch_bytes(item.url)
            record["sha256"] = _check(item, data)
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "SKILL.md").write_bytes(data)
        else:
            data = _fetch_bytes(item.url)
            record["sha256"] = _check(item, data)
            tmp = Path(tempfile.mkdtemp(prefix="hexiel-market-"))
            _safe_extract(data, tmp)
            found = next(iter(sorted(tmp.rglob("SKILL.md"))), None)
            if found is None:
                shutil.rmtree(tmp, ignore_errors=True)
                raise MarketError(f"{name}: the archive has no SKILL.md")
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(found.parent, dest)
            shutil.rmtree(tmp, ignore_errors=True)
        record["files"] = [str(dest)]
    else:                                                    # bundle (a Claude Code plugin folder)
        folder = _bundle_folder(item)
        record["sha256"] = _hash_tree(folder)
        base = _skills_dir() / name
        for skill in sorted(folder.glob("skills/*/SKILL.md")):
            shutil.copytree(skill.parent, base / skill.parent.name, dirs_exist_ok=True)
        for cmd in sorted(folder.glob("commands/*.md")):     # slash commands become skills of the same name
            text = cmd.read_text()
            if not text.lstrip().startswith("---"):
                text = f"---\nname: {cmd.stem}\ndescription: {name} command {cmd.stem}\n---\n{text}"
            (base / cmd.stem).mkdir(parents=True, exist_ok=True)
            (base / cmd.stem / "SKILL.md").write_text(text)
        if base.exists():
            record["files"] = [str(base)]
        mcp_path = folder / ".mcp.json"
        if mcp_path.is_file():
            try:
                servers = json.loads(mcp_path.read_text()).get("mcpServers") or {}
            except ValueError:
                servers = {}
            root = str(folder.resolve())
            fixed = json.loads(json.dumps(servers).replace("${CLAUDE_PLUGIN_ROOT}", root))
            record["mcp"] = _add_mcp({f"{name}-{k}": v for k, v in fixed.items()})
        for py in sorted(folder.glob("hexiel-plugins/*.py")):
            dest = _plugins_dir() / f"{name}-{py.name}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(py, dest)
            record["files"].append(str(dest))
        if not record["files"] and not record["mcp"]:
            raise MarketError(f"{name}: nothing to install (no skills/, commands/, .mcp.json or hexiel-plugins/)")
    have[name] = record
    _save("installed.json", have)
    parts = []
    if record["files"]:
        parts.append(f"{len(record['files'])} path(s)")
    if record["mcp"]:
        parts.append(f"MCP server(s) {', '.join(record['mcp'])}")
    return (f"installed {name} {item.version} ({item.kind}: {' + '.join(parts)}) — {warning(item)}. "
            "Takes effect in new sessions (or /restart).")


def _uninstall_files(rec: dict) -> None:
    for p in rec.get("files") or []:
        path = Path(p)
        allowed = (_skills_dir().resolve(), _plugins_dir().resolve())
        if not any(a == path.resolve() or a in path.resolve().parents for a in allowed):
            continue                                       # never delete outside hexiel's own folders
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    _drop_mcp(rec.get("mcp") or [])


def remove(name: str) -> str:
    have = installed()
    if name not in have:
        return f"{name} is not installed"
    _uninstall_files(have.pop(name))
    _save("installed.json", have)
    return f"removed {name}"


def updates(refresh: bool = True) -> list[tuple[str, str, str, str]]:
    """(name, installed version, available version, note) for installed items with something new."""
    have = installed()
    out = []
    for item in catalogue(refresh=refresh):
        rec = have.get(item.name)
        if not rec or item.kind == "error":
            continue
        if _vkey(item.version) > _vkey(rec.get("version", "")):
            out.append((item.name, rec.get("version", ""), item.version, ""))
        elif item.sha256 and rec.get("sha256") and item.sha256 != rec["sha256"] and item.version == rec.get("version"):
            out.append((item.name, rec["version"], item.version, "changed without a new version — check the source"))
    return out


def update(name: str = "") -> str:
    lines = []
    for n, old, new, note in updates():
        if name and n != name:
            continue
        if note:
            lines.append(f"  ! {n}: {note} (not updated; /marketplace install {n} --force to take it)")
            continue
        try:
            install(n, force=True)
            lines.append(f"  ↑ {n} {old or '?'} → {new}")
        except MarketError as exc:
            lines.append(f"  ✗ {n}: {exc}")
    return "\n".join(lines) or "everything installed is up to date"


def info(name: str) -> str:
    item = find(name)
    rec = installed().get(name)
    lines = [f"{item.name} {item.version} · {item.kind} · from {item.source}", item.description,
             f"url: {item.url}" if item.url else "", f"sha256: {item.sha256}" if item.sha256 else "unpinned (no sha256)",
             warning(item)]
    if item.kind == "mcp":
        lines.append("mcp entry: " + json.dumps(item.mcp))
    if rec:
        lines.append(f"installed {rec.get('version') or ''} on {rec.get('installed_at', '?')}")
    return "\n".join(line for line in lines if line)


# ---------------------------------------------------------------- the /marketplace command and `hexiel marketplace`

USAGE = ("/marketplace [search WORDS] | add URL [NAME] | sources | drop SOURCE | info NAME | install NAME | "
         "update [NAME] | remove NAME | installed")


def command(arg: str) -> str:
    parts = arg.split()
    verb = parts[0].lower() if parts else "search"
    rest = parts[1:]
    try:
        if verb in ("search", "list", "ls", "browse"):
            return render(search(" ".join(rest)))
        if verb == "add" and rest:
            return add_source(rest[0], rest[1] if len(rest) > 1 else "")
        if verb == "sources":
            return "\n".join(f"  {s['name']:<16} {s['url']}" for s in sources()) or "no sources — /marketplace add URL"
        if verb in ("drop", "remove-source") and rest:
            return remove_source(rest[0])
        if verb == "info" and rest:
            return info(rest[0])
        if verb == "install" and rest:
            return install(rest[0], force="--force" in rest)
        if verb in ("update", "upgrade"):
            return update(rest[0] if rest else "")
        if verb in ("remove", "uninstall", "rm") and rest:
            return remove(rest[0])
        if verb == "installed":
            have = installed()
            return "\n".join(f"  ● {n:<22} {r['kind']:<7} {r.get('version') or '-':<8} from {r.get('source')}"
                             for n, r in sorted(have.items())) or "nothing installed from the marketplace yet"
        if verb not in ("help",) and not rest:
            return render(search(arg))                      # "/marketplace pdf" = search
        return USAGE
    except MarketError as exc:
        return f"✗ {exc}"
    except Exception as exc:  # noqa: BLE001 - shown, never a crash
        from .explain import crash
        return f"✗ {crash(exc)}"


def cli(argv: list[str]) -> int:
    out = command(" ".join(argv))
    print(out)
    return 1 if out.startswith("✗") else 0


def tool_def():
    """The model can browse the marketplace (read-only); installing stays with the user (/marketplace install)."""
    from .tools import ToolDef, text_result
    return ToolDef(
        "marketplace", "Search the skills & plugins marketplace sources the user added (read-only). Suggest an item "
        "to the user with the command to install it: /marketplace install NAME.",
        {"type": "object", "properties": {"query": {"type": "string"},
                                          "info": {"type": "string", "description": "an item name for details"}}},
        readonly=True,
        run=lambda a, ctx: text_result(command(f"info {a['info']}") if a.get("info")
                                       else command("search " + str(a.get("query", "")))))
