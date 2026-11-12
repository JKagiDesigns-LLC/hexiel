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
VERSION = 3

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
ENGINE = "auto"            # resolved below: "tree-sitter" when the extra is installed, else "regex"
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


# ---------------------------------------------------------------- tree-sitter (optional extra: pip install 'hexiel[code]')

TS_LANG = {".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript",
           ".tsx": "tsx", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
           ".cs": "csharp", ".scala": "scala", ".swift": "swift", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp",
           ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".rb": "ruby", ".php": "php", ".sh": "bash", ".bash": "bash",
           ".lua": "lua", ".ex": "elixir", ".exs": "elixir", ".dart": "dart"}
# node type -> kind; "class" kinds become the parent of what they contain
TS_KINDS = {
    "function_declaration": "function", "generator_function_declaration": "function", "function_definition": "function",
    "function_item": "function", "method_definition": "method", "method_declaration": "method", "method": "method",
    "singleton_method": "method", "constructor_declaration": "method", "function_signature": "function",
    "class_declaration": "class", "class_definition": "class", "class_specifier": "class", "struct_specifier": "class",
    "struct_item": "class", "enum_item": "class", "union_item": "class", "class": "class", "object_declaration": "class",
    "record_declaration": "class", "struct_declaration": "class", "enum_declaration": "type",
    "interface_declaration": "type", "type_alias_declaration": "type", "trait_item": "type", "type_spec": "type",
    "protocol_declaration": "type", "trait_declaration": "type", "trait_definition": "type", "object_definition": "class",
    "impl_item": "impl", "module": "module", "mod_item": "module", "namespace_definition": "module",
    "namespace_declaration": "module",
}
_CONTAINERS = {"class", "impl", "module", "type"}
_NAME_TYPES = ("identifier", "type_identifier", "simple_identifier", "name", "constant", "field_identifier",
               "property_identifier", "namespace_identifier", "word")
_PARSERS: dict = {}


def treesitter_available() -> bool:
    try:
        import tree_sitter_language_pack  # noqa: F401
        return True
    except Exception:  # noqa: BLE001 - any import problem means "not installed"
        return False


def _ts_parser(lang: str):
    if lang not in _PARSERS:
        try:
            from tree_sitter_language_pack import get_parser
            _PARSERS[lang] = get_parser(lang)
        except Exception:  # noqa: BLE001 - a grammar missing from the pack: regex outline instead
            _PARSERS[lang] = None
    return _PARSERS[lang]


def _ts_name(node, src: bytes) -> str:
    field = node.child_by_field_name("name") or (node.child_by_field_name("type") if node.type == "impl_item" else None)
    if field is None and node.type in ("function_definition", "function_declaration"):     # C/C++: inside the declarator
        decl = node.child_by_field_name("declarator")
        while decl is not None and decl.child_by_field_name("declarator") is not None:
            decl = decl.child_by_field_name("declarator")
        field = decl
    if field is None:
        field = next((c for c in node.children if c.type in _NAME_TYPES), None)
    if field is None:
        return ""
    return src[field.start_byte:field.end_byte].decode("utf-8", "replace").split("(")[0].strip()[:120]


def _ts_symbols(text: str, rel: str, lang: str) -> list[Symbol] | None:
    parser = _ts_parser(lang)
    if parser is None:
        return None
    src = text.encode("utf-8")
    try:
        tree = parser.parse(src)
    except Exception:  # noqa: BLE001
        return None
    lines = text.splitlines()
    out: list[Symbol] = []

    def visit(node, parent: str, depth: int):
        if depth > 60:
            return
        for child in node.children:
            kind = TS_KINDS.get(child.type)
            if kind is None and child.type in ("lexical_declaration", "variable_declaration"):
                for decl in child.children:                 # const f = () => … / function expressions
                    value = decl.child_by_field_name("value") if decl.type == "variable_declarator" else None
                    if value is not None and value.type in ("arrow_function", "function", "function_expression"):
                        name = _ts_name(decl, src)
                        if name:
                            out.append(Symbol(name, "method" if parent else "function", rel, child.start_point[0] + 1,
                                              child.end_point[0] + 1, parent, lines[child.start_point[0]].strip()[:160],
                                              _leading_comment(lines, child.start_point[0])))
                continue
            if kind is None:
                visit(child, parent, depth + 1)
                continue
            name = _ts_name(child, src)
            if not name:
                visit(child, parent, depth + 1)
                continue
            if kind == "function" and parent:
                kind = "method"
            row = child.start_point[0]
            out.append(Symbol(name, kind, rel, row + 1, child.end_point[0] + 1, parent,
                              lines[row].strip()[:160] if row < len(lines) else "", _leading_comment(lines, row)))
            visit(child, (f"{parent}.{name}" if parent else name) if kind in _CONTAINERS else parent, depth + 1)

    visit(tree.root_node, "", 0)
    return out


def parse(text: str, rel: str) -> list[Symbol]:
    suffix = Path(rel).suffix.lower()
    lang = LANGS.get(suffix)
    if lang is None:
        return []
    if lang == "python":
        return _py_symbols(text, rel)
    if suffix in TS_LANG and ENGINE == "tree-sitter":
        got = _ts_symbols(text, rel, TS_LANG[suffix])
        if got is not None:
            return got
    return _regex_symbols(text, rel, lang)


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
        self.vectors: dict = {}            # symbol key -> float32 array (embedding), when a model is reachable
        self.vectors_model = None

    # persistence ---------------------------------------------------------
    @property
    def store(self) -> Path:
        key = hashlib.sha256(self.root.encode()).hexdigest()[:16]
        return home_data_dir() / "symbols" / f"{key}.json"

    def _load(self) -> None:
        self._loaded = True
        try:
            data = json.loads(self.store.read_text())
            if data.get("v") != VERSION or data.get("root") != self.root or data.get("engine") != ENGINE:
                return
            self.files = {rel: {"m": f["m"], "s": f["s"], "syms": [Symbol(**s) for s in f["syms"]]}
                          for rel, f in data["files"].items()}
        except (OSError, ValueError, KeyError, TypeError):
            self.files = {}

    def _save(self) -> None:
        try:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store.with_suffix(".tmp")
            tmp.write_text(json.dumps({"v": VERSION, "root": self.root, "engine": ENGINE, "files": {
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


# ---------------------------------------------------------------- meaning: embeddings when a model is there

_EMBED = {"at": 0.0, "endpoint": None}
EMBED_BATCH = 64
_VEC_LOCK = threading.Lock()


def embedder():
    """(ollama base, embedding model) when a configured Ollama server has one (docqa's discovery), else None.
    Re-checked every 5 minutes; HEXIEL_EMBED=off disables it."""
    if os.environ.get("HEXIEL_EMBED", "").lower() == "off":
        return None
    now = time.time()
    if now - _EMBED["at"] < 300:
        return _EMBED["endpoint"]
    try:
        from . import docqa
        ep = docqa._embed_endpoint(None)
    except Exception:  # noqa: BLE001 - no config, no server: keyword search
        ep = None
    _EMBED.update(at=now, endpoint=ep)
    return ep


def _embed(ep, texts: list[str]):
    from . import docqa
    return docqa._embed(ep, texts)


def _sym_key(s: Symbol) -> str:
    return hashlib.sha1(f"{s.path}|{s.qualname}|{s.signature}|{s.doc[:120]}".encode(),  # nosec B324 - a cache key
                        usedforsecurity=False).hexdigest()[:16]


def _sym_text(s: Symbol) -> str:
    return f"{s.kind} {s.qualname} in {s.path}: {s.doc or s.signature}"[:500]


def _vec_store(idx: "Index") -> Path:
    return idx.store.with_suffix(".vec.json")


def _load_vectors(idx: "Index", model: str) -> None:
    if getattr(idx, "vectors_model", None) == model:
        return
    import base64
    from array import array
    idx.vectors, idx.vectors_model = {}, model
    try:
        data = json.loads(_vec_store(idx).read_text())
        if data.get("model") == model:
            for key, b64 in data.get("vecs", {}).items():
                arr = array("f")
                arr.frombytes(base64.b64decode(b64))
                idx.vectors[key] = arr
    except (OSError, ValueError, KeyError):
        pass


def _save_vectors(idx: "Index") -> None:
    import base64
    try:
        keys = {_sym_key(s) for s in idx.all()}
        data = {"model": idx.vectors_model, "vecs": {k: base64.b64encode(v.tobytes()).decode()
                                                     for k, v in idx.vectors.items() if k in keys}}
        tmp = _vec_store(idx).with_suffix(".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data))
        os.replace(tmp, _vec_store(idx))
    except OSError:
        pass


def embed_missing(idx: "Index", ep, limit: int | None = None, symbols: list | None = None) -> int:
    """Embed definitions that have no vector yet (all of them, or `symbols`), at most `limit`. Returns how many."""
    from array import array
    with _VEC_LOCK:
        _load_vectors(idx, ep[1])
        todo = [s for s in (symbols if symbols is not None else idx.all()) if _sym_key(s) not in idx.vectors]
        if limit is not None:
            todo = todo[:limit]
        done = 0
        for i in range(0, len(todo), EMBED_BATCH):
            chunk = todo[i:i + EMBED_BATCH]
            vecs = _embed(ep, [_sym_text(s) for s in chunk])
            if not vecs or len(vecs) != len(chunk):
                break
            for sym, vec in zip(chunk, vecs):
                idx.vectors[_sym_key(sym)] = array("f", vec)
            done += len(chunk)
        if done:
            _save_vectors(idx)
        return done


def _fill_in_background(idx: "Index", ep) -> None:
    if getattr(idx, "_vec_thread", None) is not None and idx._vec_thread.is_alive():
        return
    idx._vec_thread = threading.Thread(target=lambda: embed_missing(idx, ep), name="hexiel-embed", daemon=True)
    idx._vec_thread.start()


def _cos(a, b) -> float:
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    return dot / math.sqrt(na * nb) if na and nb else 0.0


def by_meaning(idx: "Index", query: str, candidates: list, ep, top: int = 12) -> list[tuple[float, "Symbol"]]:
    """Cosine ranking: the keyword candidates (embedded now if needed, they are few) plus every definition already
    embedded; the rest are embedded in the background for next time."""
    qv = _embed(ep, [query])
    if not qv:
        return []
    embed_missing(idx, ep, limit=48, symbols=candidates)
    _fill_in_background(idx, ep)
    q = qv[0]
    out = []
    for s in idx.all():
        v = idx.vectors.get(_sym_key(s))
        if v is not None:
            out.append((_cos(q, v), s))
    out.sort(key=lambda h: -h[0])
    return out[:top]


def find(idx: Index, query: str, limit: int = 15, semantic: bool = True) -> list[tuple[float, str, str]]:
    """One call instead of a grep chain: fuzzy symbol names, fuzzy file paths, and BM25 over each symbol's
    name words + docstring/leading comment — re-ranked by meaning when an embedding model is reachable.
    Returns (score, location, what) best first."""
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
    ep = embedder() if semantic else None
    if ep:
        by_loc = {s.where(): s for s in syms}
        cands = [by_loc[loc] for _sc, loc, _w in sorted(scored.values(), key=lambda h: -h[0])[:40] if loc in by_loc]
        try:
            ranked = by_meaning(idx, query, cands, ep)
        except Exception:  # noqa: BLE001 - the embedding server went away: keyword results stand
            ranked = []
        if ranked:
            best = ranked[0][0] or 1.0
            for cos, s in ranked:
                rel_score = 55 + 40 * (cos / best)               # the closest by meaning scores like a strong name hit
                loc = s.where()
                if loc in scored:
                    sc, _l, what = scored[loc]
                    scored[loc] = (max(sc, rel_score) + 5, loc, what)   # both agree: it goes up
                else:
                    keep(rel_score, loc, f"{s.kind} {s.qualname} — {s.doc[:100] or s.signature[:100]} (by meaning)")
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


ENGINE = "tree-sitter" if os.environ.get("HEXIEL_SYMBOLS", "") != "regex" and treesitter_available() else "regex"
