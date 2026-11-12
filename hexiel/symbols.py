"""Project symbol index: where things are defined, without the model grepping for it.

Python files are parsed with `ast` (exact names, line ranges, docstrings); other languages get a regex outline
(functions, classes, types, methods) whose end lines come from brace matching or indentation. The index is built
lazily, re-parses only files whose mtime/size changed, and is persisted under the hexiel data dir so the next
session starts warm. Everything is local and pure Python — no model, no extra packages.

Tools on top (tools.py): symbols, outline, where_is, callers, find, and read(path, symbol=… | around=…)."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

from .config import home_data_dir

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pixi", "vendor", "dist", "build", "target",
             ".mypy_cache", ".ruff_cache", ".pytest_cache", ".tox", ".next", ".nuxt", "coverage", "site-packages"}
MAX_FILE = 1_000_000
MAX_FILES = 20_000
INDEX_BUDGET = 8.0          # seconds for a cold index; past it the index is partial (and says so), never a hang
VERSION = 2

LANGS = {".py": "python", ".pyi": "python", ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js", ".ts": "js",
         ".tsx": "js", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "java", ".kts": "java", ".cs": "java",
         ".scala": "java", ".swift": "java", ".c": "c", ".h": "c", ".cc": "c", ".cpp": "c", ".cxx": "c",
         ".hpp": "c", ".hh": "c", ".rb": "ruby", ".php": "php", ".sh": "shell", ".bash": "shell", ".zsh": "shell",
         ".lua": "lua", ".ex": "ruby", ".exs": "ruby", ".dart": "java", ".ps1": "shell", ".psm1": "shell"}

_RX = {
    "js": [(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\*?\s+([A-Za-z_$][\w$]*)", "function"),
           (r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)", "class"),
           (r"^\s*(?:export\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)", "type"),
           (r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:function|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)", "function"),
           (r"^\s+(?:public\s+|private\s+|protected\s+|static\s+|async\s+|readonly\s+)*([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*(?::\s*[^{]+)?\{\s*$", "method")],
    "go": [(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)", "function"),
           (r"^type\s+([A-Za-z_]\w*)\s+(?:struct|interface)", "class"),
           (r"^type\s+([A-Za-z_]\w*)\s", "type")],
    "rust": [(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?fn\s+([A-Za-z_]\w*)", "function"),
             (r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|union)\s+([A-Za-z_]\w*)", "class"),
             (r"^\s*(?:pub(?:\([^)]*\))?\s+)?trait\s+([A-Za-z_]\w*)", "type"),
             (r"^\s*impl(?:<[^>]*>)?\s+(?:[\w:<>]+\s+for\s+)?([A-Za-z_]\w*)", "impl"),
             (r"^\s*(?:pub\s+)?mod\s+([A-Za-z_]\w*)\s*\{", "module")],
    "java": [(r"^\s*(?:(?:public|private|protected|internal|static|final|abstract|sealed|open|data|partial)\s+)*"
              r"(?:class|interface|enum|record|struct|object|protocol|extension)\s+([A-Za-z_]\w*)", "class"),
             (r"^\s*(?:(?:public|private|protected|internal|static|final|abstract|override|suspend|async|virtual|open)\s+)*"
              r"(?:fun|func)\s+(?:<[^>]*>\s*)?([A-Za-z_]\w*)", "function"),
             (r"^\s*(?:(?:public|private|protected|internal|static|final|abstract|override|synchronized|async|virtual)\s+)+"
              r"[\w<>\[\],.? ]+\s+([A-Za-z_]\w*)\s*\([^;]*$", "method")],
    "c": [(r"^(?:static\s+|inline\s+|extern\s+|const\s+|unsigned\s+|signed\s+|struct\s+)*[A-Za-z_][\w:<>*& ]*?[\s*&]+"
           r"([A-Za-z_][\w:~]*)\s*\([^;]*$", "function"),
          (r"^\s*(?:typedef\s+)?(?:struct|class|union|enum)\s+([A-Za-z_]\w*)\s*(?::[^{]*)?\{?\s*$", "class"),
          (r"^\s*namespace\s+([A-Za-z_]\w*)", "module"),
          (r"^#define\s+([A-Za-z_]\w*)", "macro")],
    "ruby": [(r"^\s*def\s+(?:self\.)?([A-Za-z_]\w*[?!=]?)", "function"),
             (r"^\s*class\s+([A-Z]\w*)", "class"), (r"^\s*module\s+([A-Z]\w*)", "module"),
             (r"^\s*defp?\s+([a-z_]\w*[?!]?)", "function"), (r"^\s*defmodule\s+([\w.]+)", "module")],
    "php": [(r"^\s*(?:(?:public|private|protected|static|final|abstract)\s+)*function\s+&?([A-Za-z_]\w*)", "function"),
            (r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait|enum)\s+([A-Za-z_]\w*)", "class")],
    "shell": [(r"^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{?", "function"),
              (r"^\s*function\s+([A-Za-z_][\w-]*)", "function")],
    "lua": [(r"^\s*(?:local\s+)?function\s+([\w.:]+)", "function"),
            (r"^\s*(?:local\s+)?([\w.]+)\s*=\s*function", "function")],
}
_COMPILED = {lang: [(re.compile(rx), kind) for rx, kind in rules] for lang, rules in _RX.items()}
_C_NOT_FUNC = {"if", "for", "while", "switch", "return", "sizeof", "else", "do", "case", "catch", "new", "delete"}
_COMMENT = re.compile(r"^\s*(?:#|//|/\*+|\*|--|;)\s?(.*)$")


@dataclass
class Symbol:
    name: str
    kind: str           # function, method, class, type, module, impl, macro
    path: str           # relative to the project root
    line: int
    end: int
    parent: str = ""    # enclosing class / impl, for methods
    signature: str = ""
    doc: str = ""

    @property
    def qualname(self) -> str:
        return f"{self.parent}.{self.name}" if self.parent else self.name

    def where(self) -> str:
        return f"{self.path}:{self.line}-{self.end}"


# ---------------------------------------------------------------- parsing

def _py_symbols(text: str, rel: str) -> list[Symbol]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return _regex_symbols(text, rel, "python")
    lines = text.splitlines()
    out: list[Symbol] = []

    def sig(node) -> str:
        head = lines[node.lineno - 1].strip() if node.lineno - 1 < len(lines) else ""
        return head[:160]

    def walk(body, parent: str):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(node, ast.ClassDef) else ("method" if parent else "function")
                doc = (ast.get_docstring(node) or "").strip().split("\n\n")[0].replace("\n", " ")[:300]
                start = min([d.lineno for d in node.decorator_list] + [node.lineno])
                out.append(Symbol(node.name, kind, rel, start, getattr(node, "end_lineno", node.lineno) or node.lineno,
                                  parent, sig(node), doc))
                if isinstance(node, ast.ClassDef):
                    walk(node.body, node.name if not parent else f"{parent}.{node.name}")
            elif isinstance(node, (ast.If, ast.Try)) and not parent:      # `if TYPE_CHECKING:` / try-import blocks
                walk(node.body, parent)
    walk(tree.body, "")
    return out


def _brace_end(lines: list[str], start: int) -> int:
    """The line where the block opened at/after `start` closes (0-based in, 0-based out). Strings and comments are
    ignored roughly; a declaration with no `{` within 3 lines is one line long."""
    depth, opened = 0, False
    for i in range(start, min(len(lines), start + 5000)):
        line = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//.*$', "", lines[i])
        for ch in line:
            if ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
                if opened and depth <= 0:
                    return i
        if not opened and (i - start >= 3 or line.rstrip().endswith(";")):
            return start
    return start


def _indent_end(lines: list[str], start: int, lang: str) -> int:
    base = len(lines[start]) - len(lines[start].lstrip())
    end = start
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped:
            continue
        ind = len(lines[i]) - len(lines[i].lstrip())
        if ind <= base:
            if lang in ("ruby", "lua") and stripped.startswith("end"):
                return i
            return end
        end = i
    return end


def _regex_symbols(text: str, rel: str, lang: str) -> list[Symbol]:
    rules = _COMPILED.get(lang) or []
    if lang == "python":
        rules = [(re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)"), "function"),
                 (re.compile(r"^\s*class\s+([A-Za-z_]\w*)"), "class")]
    lines = text.splitlines()
    out: list[Symbol] = []
    classes: list[tuple[int, int, str]] = []          # (start, end, name) for parent lookup
    braces = lang in ("js", "go", "rust", "java", "c", "php", "shell")
    for i, line in enumerate(lines):
        if len(line) > 400 or not line.strip() or _COMMENT.match(line) and not line.lstrip().startswith("#define"):
            continue
        for rx, kind in rules:
            m = rx.match(line)
            if not m:
                continue
            name = m.group(1)
            if kind in ("function", "method") and name in _C_NOT_FUNC:
                break
            if lang == "c" and kind == "function" and (line.startswith((" ", "\t")) or "=" in line.split("(")[0]):
                break
            end = _brace_end(lines, i) if braces and kind != "macro" else (
                i if kind == "macro" else _indent_end(lines, i, lang))
            parent = ""
            for cs, ce, cn in reversed(classes):
                if cs < i <= ce:
                    parent = cn
                    break
            if parent and kind == "function":
                kind = "method"
            doc = _leading_comment(lines, i)
            out.append(Symbol(name, kind, rel, i + 1, end + 1, parent, line.strip()[:160], doc))
            if kind in ("class", "impl", "module"):
                classes.append((i, end, name))
            break
    return out


def _leading_comment(lines: list[str], i: int) -> str:
    got = []
    j = i - 1
    while j >= 0 and len(got) < 6:
        m = _COMMENT.match(lines[j])
        if not m or not lines[j].strip():
            break
        got.append(m.group(1).strip().rstrip("*/").strip())
        j -= 1
    return " ".join(reversed([g for g in got if g]))[:300]


def parse(text: str, rel: str) -> list[Symbol]:
    lang = LANGS.get(Path(rel).suffix.lower())
    if lang is None:
        return []
    return _py_symbols(text, rel) if lang == "python" else _regex_symbols(text, rel, lang)


# ---------------------------------------------------------------- the index

class Index:
    """Symbols for one project root. `refresh()` is cheap when nothing changed (a stat walk);
    changed files are re-parsed. Thread-safe; persisted between sessions."""

    def __init__(self, root: str):
        self.root = str(Path(root).resolve())
        self.files: dict[str, dict] = {}          # rel -> {"m": mtime_ns, "s": size, "syms": [Symbol]}
        self.partial = False
        self.last_refresh = 0.0
        self.lock = threading.Lock()
        self._loaded = False

    # persistence ---------------------------------------------------------
    @property
    def store(self) -> Path:
        key = hashlib.sha256(self.root.encode()).hexdigest()[:16]
        return home_data_dir() / "symbols" / f"{key}.json"

    def _load(self) -> None:
        self._loaded = True
        try:
            data = json.loads(self.store.read_text())
            if data.get("v") != VERSION or data.get("root") != self.root:
                return
            self.files = {rel: {"m": f["m"], "s": f["s"], "syms": [Symbol(**s) for s in f["syms"]]}
                          for rel, f in data["files"].items()}
        except (OSError, ValueError, KeyError, TypeError):
            self.files = {}

    def _save(self) -> None:
        try:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store.with_suffix(".tmp")
            tmp.write_text(json.dumps({"v": VERSION, "root": self.root, "files": {
                rel: {"m": f["m"], "s": f["s"], "syms": [asdict(s) for s in f["syms"]]} for rel, f in self.files.items()}}))
            os.replace(tmp, self.store)
        except OSError:
            pass

    # walking -------------------------------------------------------------
    def _walk(self):
        count = 0
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            for name in filenames:
                if Path(name).suffix.lower() not in LANGS:
                    continue
                count += 1
                if count > MAX_FILES:
                    self.partial = True
                    return
                yield os.path.join(dirpath, name)

    def refresh(self, budget: float = INDEX_BUDGET, min_interval: float = 1.0) -> "Index":
        with self.lock:
            if not self._loaded:
                self._load()
            now = time.time()
            if now - self.last_refresh < min_interval and self.files:
                return self
            deadline = now + budget
            seen, changed = set(), False
            self.partial = False
            for full in self._walk():
                rel = os.path.relpath(full, self.root)
                seen.add(rel)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                cur = self.files.get(rel)
                if cur and cur["m"] == st.st_mtime_ns and cur["s"] == st.st_size:
                    continue
                if time.time() > deadline:
                    self.partial = True
                    continue
                if st.st_size > MAX_FILE:
                    syms = []
                else:
                    try:
                        with open(full, "rb") as fh:
                            raw = fh.read()
                        syms = [] if b"\x00" in raw[:4000] else parse(raw.decode("utf-8", "replace"), rel)
                    except OSError:
                        continue
                self.files[rel] = {"m": st.st_mtime_ns, "s": st.st_size, "syms": syms}
                changed = True
            for rel in [r for r in self.files if r not in seen]:
                del self.files[rel]
                changed = True
            self.last_refresh = time.time()
            if changed and not self.partial:
                self._save()
            return self

    def all(self) -> list[Symbol]:
        return [s for f in self.files.values() for s in f["syms"]]

    def outline(self, rel: str) -> list[Symbol]:
        rel = self.rel(rel)
        f = self.files.get(rel)
        if f is None:
            full = os.path.join(self.root, rel)
            try:
                return parse(Path(full).read_text(errors="replace"), rel)
            except OSError:
                return []
        return f["syms"]

    def rel(self, path: str) -> str:
        p = Path(path)
        if p.is_absolute():
            try:
                return os.path.relpath(p.resolve(), self.root)
            except ValueError:
                return str(p)
        return os.path.normpath(path)


_INDEXES: dict[str, Index] = {}
_LOCK = threading.Lock()


def index_for(root: str) -> Index:
    key = str(Path(root).resolve())
    with _LOCK:
        idx = _INDEXES.get(key)
        if idx is None:
            idx = _INDEXES[key] = Index(key)
    return idx.refresh()


PROJECT_MARKERS = (".git", "pyproject.toml", "package.json", "go.mod", "Cargo.toml", "pom.xml", "build.gradle",
                   "Gemfile", "composer.json", "CMakeLists.txt", "Makefile", "setup.py", ".hexiel")


WARM_ENABLED = True


def warm(root: str) -> None:
    """Build the index in the background at startup so the first lookup is instant — only in a folder that looks
    like a project (never the home directory or /), and never twice."""
    if not WARM_ENABLED:
        return
    home = Path.home().resolve()
    here = Path(root).resolve()
    if here in (home, Path(here.anchor)) or not any((here / m).exists() for m in PROJECT_MARKERS):
        return
    if str(here) in _INDEXES:
        return
    threading.Thread(target=lambda: index_for(root), name="hexiel-symbols", daemon=True).start()


# ---------------------------------------------------------------- matching

@lru_cache(maxsize=65536)
def _words(name: str) -> tuple[str, ...]:
    return tuple(_split_words(name))


def _split_words(name: str) -> list[str]:
    parts = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|\b)|[A-Z]?[a-z]+|[A-Z]+|\d+", name)
    return [p.lower() for p in parts] or [name.lower()]


def fuzzy(query: str, name: str) -> float:
    """0..100: how well `name` matches `query` — exact, prefix, substring, word overlap (snake/camel aware),
    initials ("gcf" → get_config_file) and typo tolerance (SequenceMatcher)."""
    q, n = query.lower().strip(), name.lower()
    if not q:
        return 0.0
    if q == n:
        return 100.0
    if n.startswith(q):
        return 92.0 - min(len(n) - len(q), 20) * 0.3
    if q in n:
        return 84.0 - min(len(n) - len(q), 30) * 0.3
    qw, nw = _words(query), _words(name)
    overlap = len(set(qw) & set(nw)) / max(len(set(qw)), 1)
    initials = "".join(w[0] for w in nw)
    score = max(overlap * 80.0, 78.0 if q == initials else 0.0)
    sm = SequenceMatcher(None, q, n, autojunk=False)
    if sm.real_quick_ratio() * 75.0 > max(score, 40.0) and sm.quick_ratio() * 75.0 > max(score, 40.0):
        score = max(score, sm.ratio() * 75.0)          # the full edit-similarity only when it could matter
    if score < 74.0 and len(q) >= 4 and all(
            any(abs(len(a) - len(b)) <= 2 and SequenceMatcher(None, a, b).ratio() >= 0.8 for b in nw) for a in qw):
        score = 74.0                                   # every query word matches a word with a typo
    return score


def lookup(idx: Index, name: str, limit: int = 20, kinds: set | None = None, min_score: float = 60.0) -> list[tuple[float, Symbol]]:
    hits = []
    qual = "." in name
    for s in idx.all():
        if kinds and s.kind not in kinds:
            continue
        sc = fuzzy(name, s.qualname if qual else s.name)
        if sc >= min_score:
            hits.append((sc, s))
    hits.sort(key=lambda h: (-h[0], len(h[1].path), h[1].path, h[1].line))
    return hits[:limit]


_IDENT = re.compile(r"^[A-Za-z_$][\w$]*$")


def callers(idx: Index, name: str, limit: int = 60) -> list[str]:
    """Lines that call or reference `name` (a word match, excluding its own definitions), with the enclosing
    symbol so the model sees *who* calls it."""
    short = name.split(".")[-1]
    if not _IDENT.match(short):
        return []
    rx = re.compile(rf"(?<![\w$]){re.escape(short)}(?![\w$])")
    defs = {(s.path, s.line) for s in idx.all() if s.name == short}
    out = []
    for rel, f in sorted(idx.files.items()):
        try:
            text = Path(idx.root, rel).read_text(errors="replace")
        except OSError:
            continue
        if short not in text:
            continue
        syms = f["syms"]
        for i, line in enumerate(text.splitlines(), 1):
            if (rel, i) in defs or not rx.search(line):
                continue
            stripped = line.strip()
            if stripped.startswith(("def ", "class ", "async def ")) and short in stripped.split("(")[0]:
                continue
            inside = min((s for s in syms if s.line <= i <= s.end), key=lambda s: s.end - s.line, default=None)
            where = f" (in {inside.qualname})" if inside else ""
            out.append(f"{rel}:{i}{where}: {stripped[:200]}")
            if len(out) >= limit:
                return out
    return out


# ---------------------------------------------------------------- find: names + words in one call

_TOKEN = re.compile(r"[A-Za-z][a-z]+|[A-Z]+(?![a-z])|\d+")


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text) if len(t) > 1]


def find(idx: Index, query: str, limit: int = 15) -> list[tuple[float, str, str]]:
    """One call instead of a grep chain: fuzzy symbol names, fuzzy file paths, and BM25 over each symbol's
    name words + docstring/leading comment. Returns (score, location, what) best first."""
    syms = idx.all()
    qtok = _tokens(query) or [query.lower()]
    docs = [(_tokens(s.name) + _tokens(s.parent) + _tokens(s.doc)) for s in syms]
    n = len(docs) or 1
    avg = sum(len(d) for d in docs) / n or 1.0
    df: dict[str, int] = {}
    for d in docs:
        for t in set(d):
            df[t] = df.get(t, 0) + 1
    scored: dict[str, tuple[float, str, str]] = {}

    def keep(score, loc, what):
        if loc not in scored or scored[loc][0] < score:
            scored[loc] = (score, loc, what)

    k1, b = 1.4, 0.75
    bm = []
    for s, d in zip(syms, docs):
        if not d:
            continue
        score = 0.0
        for t in qtok:
            tf = d.count(t)
            if not tf:
                continue
            idf = math.log(1 + (n - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5))
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(d) / avg))
        if score:
            bm.append((score, s))
    top_bm = max((x[0] for x in bm), default=1.0) or 1.0
    for score, s in bm:
        keep(60 + 30 * score / top_bm, s.where(), f"{s.kind} {s.qualname} — {s.doc[:100] or s.signature[:100]}")
    compact = query.replace(" ", "_")
    for score, s in lookup(idx, compact, limit=40, min_score=62):
        keep(score, s.where(), f"{s.kind} {s.qualname} — {s.signature[:100]}")
    for rel in idx.files:
        stem = Path(rel).stem
        sc = max(fuzzy(compact, stem), fuzzy(query, rel) * 0.9)
        if sc >= 70:
            keep(sc - 2, rel, "file")
    return sorted(scored.values(), key=lambda h: -h[0])[:limit]


# ---------------------------------------------------------------- formatting (shared by the tools)

def fmt(sym: Symbol, score: float | None = None) -> str:
    extra = f"  — {sym.doc[:120]}" if sym.doc else ""
    return f"{sym.where():40} {sym.kind:8} {sym.qualname}{extra}"


def outline_text(idx: Index, rel: str) -> str:
    syms = idx.outline(rel)
    if not syms:
        return f"{rel}: no symbols found (unsupported language, or nothing defined)"
    lines = [f"{rel} — {len(syms)} symbols (read path=… symbol=NAME to read one):"]
    for s in syms:
        pad = "  " * (s.parent.count(".") + 1 if s.parent else 0)
        doc = f"  — {s.doc[:90]}" if s.doc else ""
        lines.append(f"{s.line:>6}-{s.end:<6} {pad}{s.kind} {s.name}{doc}")
    return "\n".join(lines)


def symbol_range(idx: Index, rel: str, name: str) -> tuple[int, int] | None:
    """Line range (1-based, inclusive) of `name` (or Parent.name) in one file."""
    syms = idx.outline(rel)
    for s in syms:
        if s.qualname == name or (s.name == name and "." not in name):
            return s.line, s.end
    best = max(syms, key=lambda s: fuzzy(name, s.qualname), default=None)
    if best and fuzzy(name, best.qualname) >= 80:
        return best.line, best.end
    return None
