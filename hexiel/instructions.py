"""Agent instruction files, made small: find CLAUDE.md / AGENTS.md / .cursorrules / Claude memories…,
split them verbatim into topic files under .hexiel/instructions/, and keep a short INDEX.md that the
system prompt loads instead of the whole document. Small-context models then pay for an index plus the
one or two topics a request actually needs (see `relevant`), not for every rule in the project.

Nothing is summarised or rewritten: topic bodies are exact slices of the original, and `verify` proves
that before any original is touched. The whole flow is prompted once per project (`offer_import`).

  <project>/.hexiel/instructions/INDEX.md      always loaded (capped); lists the topics
  <project>/.hexiel/instructions/<src>-<topic>.md   one slice of one source file (frontmatter + verbatim body)
  <project>/.hexiel/imported/<file>            backup of every file we replaced
  <project>/.hexiel/import-state.json          what was imported / declined, and the source-control choice
"""

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import home_config_dir

INDEX_NAME = "INDEX.md"
STUB_MARK = "<!-- hexiel:stub -->"
INDEX_MARK = "<!-- hexiel:index -->"
MIN_CHUNK = 700               # chars; smaller sections are merged into their neighbour
INDEX_CAP = 3000              # chars of INDEX.md loaded into the system prompt
CORE_CAP = 1500               # chars of "core" rules inlined in the index
CORE_RE = re.compile(r"\b(rules?|important|never|always|must|commands?|build|test(?:s|ing)?|setup)\b", re.I)
SYNC_SKIP = {"MEMORY.md", "hexiel-memory-sync.md", "claude-memory-sync.md"}
STOPWORDS = set("the and for that with this from have into your what when where which how are not you can "
                "use using make need want please just should would could there their about also then".split())

ROOT_FILES = ("claude.md", "agents.md", "hexiel.md", "gemini.md", ".cursorrules", ".windsurfrules",
              ".clinerules", "copilot.md", ".agent.md")


@dataclass
class Source:
    path: Path
    scope: str                 # "project" | "global"
    label: str                 # slug used in topic file names: claude, agents, cursorrules…
    text: str = ""
    hash: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def tool(self) -> str:
        return self.label


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "section"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def instructions_dir(cwd: str) -> Path:
    return Path(cwd) / ".hexiel" / "instructions"


def global_instructions_dir() -> Path:
    return home_config_dir() / "instructions"


def state_file(cwd: str) -> Path:
    return Path(cwd) / ".hexiel" / "import-state.json"


def load_state(cwd: str) -> dict:
    try:
        return json.loads(state_file(cwd).read_text())
    except (OSError, ValueError):
        return {}


def save_state(cwd: str, state: dict) -> None:
    path = state_file(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


def is_generated(text: str) -> bool:
    head = text[:400]
    return STUB_MARK in head or INDEX_MARK in head


# ── discovery ──────────────────────────────────────────────────────────────────

def discover(cwd: str) -> list[Source]:
    """Instruction files every coding agent uses (claude, codex, cursor, gemini, copilot, …) or an
    old hexiel left here, that are not yet generated stubs/indexes. Layered: every harness's rules
    are imported together; when one harness keeps two copies of its own file (root + nested), the
    later one wins — the nested .claude/CLAUDE.md beats a stray root CLAUDE.md, per scope."""
    found: list[tuple[Path, str, str]] = []
    root = Path(cwd)
    try:
        for p in sorted(root.iterdir()):
            if p.is_file() and p.name.lower() in ROOT_FILES:
                found.append((p, "project", p.name.lower().lstrip(".").removesuffix(".md")))
    except OSError:
        pass
    for rel, label in ((".claude/CLAUDE.md", "claude"), (".codex/AGENTS.md", "codex"),
                       (".github/copilot-instructions.md", "copilot")):
        if (root / rel).is_file():
            found.append((root / rel, "project", label))
    for harness in (".cursor", ".codex"):
        rules = root / harness / "rules"
        if rules.is_dir():
            short = harness.lstrip(".")
            found += [(root / harness / "rules" / p.name, "project",
                       f"{short}-" + _slug(p.stem))
                      for p in sorted(rules.glob("*.md*")) if p.is_file()]
    home = Path.home()
    for p, label in ((home / ".claude" / "CLAUDE.md", "claude"), (home / ".codex" / "AGENTS.md", "codex"),
                     (home_config_dir() / "HEXIEL.md", "hexiel")):
        if p.is_file():
            found.append((p, "global", label))
    out: list[Source] = []
    seen_by_scope: dict[str, dict] = {}      # scope -> label -> Source (later file beats the earlier)
    for path, scope, label in found:
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if not text.strip() or is_generated(text):
            continue
        # "last wins": a later same-label file in the same scope replaces the earlier one
        took = seen_by_scope.get(scope, {}).pop(label, None)
        if took is not None:
            out.remove(took)
        src = Source(path, scope, label, text, _hash(text))
        out.append(src)
        seen_by_scope.setdefault(scope, {})[label] = src
    return out


# ── splitting ──────────────────────────────────────────────────────────────────

def split_sections(text: str) -> list[tuple[str, str]]:
    """(title, body) pairs; concatenating the bodies gives `text` back exactly. Splits on `## ` headings
    outside code fences; whatever precedes the first heading is the 'overview'."""
    lines = text.splitlines(keepends=True)
    sections: list[tuple[str, list[str]]] = [("overview", [])]
    fenced = False
    for line in lines:
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        if not fenced and re.match(r"##\s+\S", line):
            sections.append((line.lstrip("#").strip(), [line]))
        else:
            sections[-1][1].append(line)
    return [(t, "".join(b)) for t, b in sections if "".join(b)]


def chunk(text: str) -> list[tuple[str, str]]:
    """Sections merged up to MIN_CHUNK so we never emit dozens of tiny files."""
    out: list[tuple[str, str]] = []
    for title, body in split_sections(text):
        if out and (len(out[-1][1]) < MIN_CHUNK or len(body) < MIN_CHUNK // 2):
            prev_title, prev_body = out[-1]
            out[-1] = (prev_title if len(prev_body) >= len(body) else title, prev_body + body)
        else:
            out.append((title, body))
    return out


def _describe(title: str, body: str) -> str:
    for line in body.splitlines():
        s = line.strip().lstrip("#-*> ").strip()
        if len(s) > 12 and s.lower() != title.lower():
            return s[:110]
    return title[:110]


def write_topics(src: Source, dest: Path) -> list[Path]:
    """Write one verbatim topic file per chunk of `src` and return them."""
    dest.mkdir(parents=True, exist_ok=True)
    chunks = chunk(src.text)
    written: list[Path] = []
    used: set[str] = set()
    for title, body in chunks:
        stem = src.label if len(chunks) == 1 else f"{src.label}-{_slug(title)}"
        name, n = stem, 2
        while name in used:
            name, n = f"{stem}-{n}", n + 1
        used.add(name)
        meta = (f"---\nname: {name}\ndescription: {_describe(title, body)}\nsource: {src.path.name}\n"
                f"source_hash: {src.hash}\ntitle: {title}\n---\n\n")
        path = dest / f"{name}.md"
        path.write_text(meta + body)
        written.append(path)
    return written


def read_topic(path: Path) -> tuple[dict, str]:
    text = path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n\n", text, re.S)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip()
    return meta, text[m.end():]


def verify(src: Source, files: list[Path]) -> bool:
    """The topic bodies, in order, reproduce the source byte for byte."""
    try:
        return "".join(read_topic(p)[1] for p in files) == src.text
    except OSError:
        return False


# ── index ─────────────────────────────────────────────────────────────────────

def build_index(dest: Path, base: str) -> str:
    """INDEX.md text for every topic file in `dest`; `base` is the path prefix shown to the model."""
    topics = []
    for p in sorted(dest.glob("*.md")):
        if p.name == INDEX_NAME:
            continue
        meta, body = read_topic(p)
        topics.append((p, meta, body))
    lines = [INDEX_MARK, "# Project instructions (index)", "",
             "Instructions are split into small topic files. Read a topic with the `read` tool "
             "before doing work it covers; the ones matching your request are attached automatically.", ""]
    core, core_len = [], 0
    for p, meta, body in topics:
        if CORE_RE.search(meta.get("title", "")) and len(body) <= 600 and core_len + len(body) <= CORE_CAP:
            core.append((meta.get("title", p.stem), body.strip()))
            core_len += len(body)
    for title, body in core:
        lines += [f"## Core: {title}", body, ""]
    lines.append("## Topics")
    for p, meta, body in topics:
        lines.append(f"- `{base}{p.name}` — {meta.get('description') or p.stem}")
    return "\n".join(lines) + "\n"


def refresh_index(dest: Path, base: str) -> None:
    (dest / INDEX_NAME).write_text(build_index(dest, base))


def prompt_block(cwd: str) -> str:
    """Project + global INDEX.md for the system prompt ('' when nothing was imported)."""
    parts = []
    for d in (global_instructions_dir(), instructions_dir(cwd)):
        try:
            text = (d / INDEX_NAME).read_text()
        except OSError:
            continue
        text = text.replace(INDEX_MARK + "\n", "", 1).strip()
        if len(text) > INDEX_CAP:
            text = text[:INDEX_CAP] + f"\n[… index truncated — full list in {d / INDEX_NAME}]"
        parts.append(text)
    return "\n\n".join(parts)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9_.-]{3,}", text.lower()) if w not in STOPWORDS}


def relevant(cwd: str, request: str, window: int, already: set[str] | None = None, k: int = 2) -> str:
    """Topic bodies that match `request`, within a budget that scales with the model's context window
    (about 2% of it, 1.5k–8k chars). `already` collects names sent earlier so they are not repeated."""
    want = _words(request)
    if not want:
        return ""
    budget = max(1500, min(8000, int(window * 4 * 0.02)))
    scored = []
    for d in (instructions_dir(cwd), global_instructions_dir()):
        for p in sorted(d.glob("*.md")) if d.is_dir() else []:
            if p.name == INDEX_NAME or (already is not None and p.name in already):
                continue
            try:
                meta, body = read_topic(p)
            except OSError:
                continue
            head = _words(meta.get("title", "") + " " + meta.get("description", "") + " " + p.stem)
            score = 3 * len(want & head) + len(want & _words(body[:2000]))
            if score >= 2:
                scored.append((score, p, body))
    out, used = [], 0
    for _, p, body in sorted(scored, key=lambda s: -s[0])[:k]:
        body = body.strip()[:max(0, budget - used)]
        if not body:
            break
        used += len(body)
        out.append(f"### {p.name}\n{body}")
        if already is not None:
            already.add(p.name)
    return "\n\n".join(out)


# ── source-control choice ─────────────────────────────────────────────────────

def vcs_kind(cwd: str) -> str:
    root = Path(cwd)
    if (root / ".svn").exists():
        return "svn"
    r = subprocess.run(["git", "-C", cwd, "rev-parse", "--git-dir"], capture_output=True, text=True)
    return "git" if r.returncode == 0 else ""


def _tracked(cwd: str, path: Path) -> bool:
    r = subprocess.run(["git", "-C", cwd, "ls-files", "--error-unmatch", str(path)], capture_output=True)
    return r.returncode == 0


def add_ignores(cwd: str, names: list[str], directory: str = "") -> str:
    """Ignore `names` (relative to `directory`, default the project) in git or svn; returns a one-line note."""
    kind = vcs_kind(cwd)
    base = Path(cwd) / directory
    if kind == "git":
        gi = Path(cwd) / ".gitignore"
        have = gi.read_text().splitlines() if gi.exists() else []
        rel = [f"{directory}/{n}".lstrip("/") for n in names]
        new = [r for r in rel if r not in have]
        if new:
            sep = "" if not have or gi.read_text().endswith("\n") else "\n"
            gi.write_text((gi.read_text() if gi.exists() else "") + sep + "\n".join(new) + "\n")
        return f"added {', '.join(new) or 'nothing new'} to .gitignore"
    if kind == "svn":
        base.mkdir(parents=True, exist_ok=True)
        cur = subprocess.run(["svn", "propget", "svn:ignore", str(base)], capture_output=True, text=True)
        have = cur.stdout.split() if cur.returncode == 0 else []
        merged = have + [n for n in names if n not in have]
        r = subprocess.run(["svn", "propset", "svn:ignore", "\n".join(merged), str(base)],
                           capture_output=True, text=True)
        return (f"set svn:ignore on {base.name or '.'}: {', '.join(names)}" if r.returncode == 0
                else f"could not set svn:ignore ({r.stderr.strip()[:80]})")
    return "no git/svn repository detected — nothing to ignore"


# ── memories ──────────────────────────────────────────────────────────────────

def claude_memory_dir(cwd: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd).resolve()))
    return Path.home() / ".claude" / "projects" / slug / "memory"


def _append_index(directory: Path, name: str, meta_desc: str) -> None:
    index = directory / "MEMORY.md"
    cur = index.read_text() if index.exists() else ""
    if f"]({name})" in cur:
        return
    title = name.removesuffix(".md").replace("-", " ").capitalize()
    index.write_text(cur + ("" if not cur or cur.endswith("\n") else "\n") + f"- [{title}]({name}) — {meta_desc}\n")


def _desc(path: Path) -> str:
    m = re.search(r"^description:\s*(.+)$", path.read_text(), re.M)
    return (m.group(1).strip() if m else path.stem)[:110]


def sync_memories(cwd: str) -> dict:
    """Copy memory files between Claude's project memory and hexiel's in both directions: files missing on
    one side are copied; when both differ the newer file wins. MEMORY.md indexes are kept in step."""
    claude, hexiel = claude_memory_dir(cwd), Path(cwd) / ".hexiel" / "memory"
    counts = {"to_hexiel": 0, "to_claude": 0}
    if not claude.is_dir() and not hexiel.is_dir():
        return counts
    for d in (claude, hexiel):
        d.mkdir(parents=True, exist_ok=True)
    names = {p.name for d in (claude, hexiel) for p in d.glob("*.md")} - SYNC_SKIP
    for name in sorted(names):
        a, b = claude / name, hexiel / name
        if a.exists() and (not b.exists() or (a.read_bytes() != b.read_bytes() and a.stat().st_mtime > b.stat().st_mtime)):
            shutil.copy2(a, b)
            counts["to_hexiel"] += 1
        elif b.exists() and (not a.exists() or (a.read_bytes() != b.read_bytes() and b.stat().st_mtime > a.stat().st_mtime)):
            shutil.copy2(b, a)
            counts["to_claude"] += 1
        for d in (claude, hexiel):
            _append_index(d, name, _desc(d / name))
    return counts


def write_bridge_memories(cwd: str) -> list[Path]:
    """Tell each tool about the other's memory so a user running both keeps them in step."""
    claude, hexiel = claude_memory_dir(cwd), Path(cwd) / ".hexiel" / "memory"
    for d in (claude, hexiel):
        d.mkdir(parents=True, exist_ok=True)
    claude_note = (
        "---\nname: hexiel-memory-sync\n"
        "description: this project is also worked on with hexiel; keep both tools' memories and instructions in step\n"
        "metadata:\n  type: reference\n---\n\n"
        f"The user also runs **hexiel** (a coding agent) in `{cwd}`. hexiel keeps memories in "
        f"`{hexiel}` (same file format as these) and project instructions, split into small topic files, in "
        f"`{instructions_dir(cwd)}` (start at INDEX.md; AGENTS.md points there).\n\n"
        "**Why:** whichever tool saved a fact last should not be the only one that knows it.\n\n"
        f"**How to apply:** when you save or change a memory here, also write the same file to `{hexiel}` "
        "(or run `hexiel instructions sync`); read hexiel's memories there when you need context that is "
        "not in this directory. When project rules change, update the topic file in "
        "`.hexiel/instructions/` rather than growing CLAUDE.md.\n")
    hexiel_note = (
        "---\nname: claude-memory-sync\n"
        "description: this project is also worked on with Claude Code; keep both tools' memories in step\n"
        "metadata:\n  type: reference\n---\n\n"
        f"The user also runs Claude Code in `{cwd}`; its memories live in `{claude}` (same file format). "
        "`hexiel instructions sync` copies new and newer memory files both ways.\n\n"
        f"**How to apply:** when you save or change a memory here, also write it to `{claude}`, or run the "
        "sync command.\n")
    paths = []
    for d, name, text in ((claude, "hexiel-memory-sync.md", claude_note), (hexiel, "claude-memory-sync.md", hexiel_note)):
        (d / name).write_text(text)
        _append_index(d, name, _desc(d / name))
        paths.append(d / name)
    return paths


# ── import / restore ──────────────────────────────────────────────────────────

STUB = (STUB_MARK + "\n# Moved\n\nThis project's agent instructions are kept in small topic files so that agents "
        "with small context windows can load only what they need.\n\n"
        "- Start with `AGENTS.md` (the index), then read the topic file you need in `.hexiel/instructions/`.\n"
        "- Memories: `.hexiel/memory/` (an index is in `MEMORY.md`).\n"
        "- The original of this file is saved at `.hexiel/imported/{name}`.\n")


def import_sources(cwd: str, sources: list[Source], vcs: str, say=print) -> dict:
    """Split `sources` into topic files. `vcs` is "ignore" (keep everything out of source control, leave the
    original files alone) or "commit" (replace the originals with pointers; the topic files get committed)."""
    root = Path(cwd)
    state = load_state(cwd)
    imported = state.setdefault("imported", {})
    result = {"topics": 0, "replaced": [], "skipped": []}
    replace = vcs == "commit"
    for src in sources:
        dest = instructions_dir(cwd) if src.scope == "project" else global_instructions_dir()
        files = write_topics(src, dest)
        if not verify(src, files):                       # never touch an original we could not reproduce
            for f in files:
                f.unlink(missing_ok=True)
            result["skipped"].append(f"{src.path.name} (verification failed)")
            continue
        result["topics"] += len(files)
        imported[str(src.path)] = src.hash
        if replace and src.scope == "project":
            backup = root / ".hexiel" / "imported" / src.path.relative_to(root).as_posix().replace("/", "__")
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src.path, backup)
            if src.path.name.lower() != "agents.md":
                src.path.write_text(STUB.format(name=backup.name))
            result["replaced"].append(src.path.relative_to(root).as_posix())
    for scope, dest, base in (("project", instructions_dir(cwd), ".hexiel/instructions/"),
                              ("global", global_instructions_dir(), str(global_instructions_dir()) + "/")):
        if dest.is_dir() and any(dest.glob("*.md")):
            refresh_index(dest, base)
    if replace and (instructions_dir(cwd) / INDEX_NAME).exists():
        existing = next((p for p in root.iterdir() if p.name.lower() == "agents.md"), root / "AGENTS.md")
        existing.write_text((instructions_dir(cwd) / INDEX_NAME).read_text())
    state["vcs"] = vcs
    say(add_ignores(cwd, [".hexiel"]) if vcs == "ignore" else
        add_ignores(cwd, ["imported", "pastes"], directory=".hexiel"))
    save_state(cwd, state)
    return result


def restore(cwd: str) -> list[str]:
    """Put back every original file replaced by a stub (from .hexiel/imported/)."""
    back = Path(cwd) / ".hexiel" / "imported"
    restored = []
    for f in sorted(back.glob("*")) if back.is_dir() else []:
        target = Path(cwd) / f.name.replace("__", "/")
        shutil.copy2(f, target)
        restored.append(target.name)
    return restored


def pending(cwd: str) -> list[Source]:
    """Sources worth offering: not imported yet at their current content, and not declined."""
    state = load_state(cwd)
    seen = {**state.get("imported", {}), **state.get("declined", {})}
    return [s for s in discover(cwd) if seen.get(str(s.path)) != s.hash]


def claude_memory_pending(cwd: str) -> bool:
    d = claude_memory_dir(cwd)
    return d.is_dir() and any(p.name not in SYNC_SKIP and not (Path(cwd) / ".hexiel" / "memory" / p.name).exists()
                              for p in d.glob("*.md"))


def offer_import(cwd: str, ask=input, say=print) -> bool:
    """The one-time prompt. Returns True when something was imported."""
    sources = pending(cwd)
    mem = claude_memory_pending(cwd) and not load_state(cwd).get("memory_declined")
    if not sources and not mem:
        return False
    say("\nhexiel found agent instruction files it can reorganise into small topic files "
        "(easier on models with small context windows):")
    for s in sources:
        say(f"  · {s.path}  ({len(s.text):,} chars)")
    if mem:
        say(f"  · Claude Code memories in {claude_memory_dir(cwd)}")
    if ask("Import them now? Nothing is rewritten, only split — originals are backed up. [Y/n] ").strip().lower() in ("n", "no"):
        state = load_state(cwd)
        state.setdefault("declined", {}).update({str(s.path): s.hash for s in sources})
        if mem:
            state["memory_declined"] = True
        save_state(cwd, state)
        say("ok — not asking again unless those files change (`hexiel instructions import` to do it later).")
        return False
    vcs_name = vcs_kind(cwd) or "source control"
    choice = ask(f"Keep these out of {vcs_name}, or let them be committed?\n"
                 "  [i] ignore  — add .hexiel to the ignore list, leave your existing files untouched\n"
                 "  [c] commit  — AGENTS.md becomes the short index, CLAUDE.md etc. become pointers, "
                 "topic files are committed\n[i/c] ").strip().lower()
    vcs = "commit" if choice.startswith("c") else "ignore"
    run_import(cwd, sources, vcs, say)
    return True


def run_import(cwd: str, sources: list[Source], vcs: str, say=print) -> None:
    if sources:
        res = import_sources(cwd, sources, vcs, say)
        say(f"split into {res['topics']} topic file(s) in .hexiel/instructions/"
            + (f"; replaced {', '.join(res['replaced'])} (backups in .hexiel/imported/)" if res["replaced"] else ""))
        for s in res["skipped"]:
            say(f"  skipped {s}")
    if claude_memory_dir(cwd).is_dir() or (Path(cwd) / ".hexiel" / "memory").is_dir():
        c = sync_memories(cwd)
        write_bridge_memories(cwd)
        say(f"memories: {c['to_hexiel']} copied into hexiel, {c['to_claude']} into Claude's; "
            "each tool now has a note about the other.")


def cli(argv: list[str]) -> int:
    import os
    cwd = os.getcwd()
    cmd = argv[0] if argv else "status"
    if cmd == "scan":
        for s in discover(cwd):
            print(f"{s.scope:8} {s.path}  {len(s.text):,} chars  {len(chunk(s.text))} topic(s)")
        print(f"claude memories: {claude_memory_dir(cwd)} ({'found' if claude_memory_dir(cwd).is_dir() else 'none'})")
        return 0
    if cmd == "import":
        if "--yes" in argv:
            vcs = "commit" if "--commit" in argv else "ignore"
            run_import(cwd, discover(cwd), vcs)
        else:
            state = load_state(cwd)
            state.pop("declined", None)
            state.pop("memory_declined", None)
            save_state(cwd, state)
            if not offer_import(cwd):
                print("nothing to import")
        return 0
    if cmd == "sync":
        c = sync_memories(cwd)
        write_bridge_memories(cwd)
        print(f"{c['to_hexiel']} memory file(s) copied into hexiel, {c['to_claude']} into Claude's")
        return 0
    if cmd == "restore":
        print("restored: " + (", ".join(restore(cwd)) or "nothing (no backups)"))
        return 0
    d = instructions_dir(cwd)
    n = len([p for p in d.glob("*.md") if p.name != INDEX_NAME]) if d.is_dir() else 0
    print(f"{n} instruction topic(s) in {d}\nstate: {state_file(cwd)} {load_state(cwd).get('vcs', '(not imported)')}")
    print("commands: scan | import [--yes [--commit]] | sync | restore")
    return 0
