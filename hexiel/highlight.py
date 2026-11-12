"""Syntax colours for code and structured files shown in the terminal UI and the web UI.

Tree-sitter (the optional `hexiel[code]` extra) gives exact tokens for 30+ languages — JSON keys apart from values,
function and type names where they are defined, strings with escapes kept whole. Without it Pygments (which the
terminal UI already depends on) colours the same categories, so there is always a highlight and never a crash.

    spans(text, lang)      → [(start, end, category)] over the characters of `text`, sorted, non-overlapping
    rich_markup(text, lang) / html(text, lang)  → the same spans as Rich markup / escaped HTML with <span class="tok-…">
    lang_for(path_or_text) → a language name from a file name (or by sniffing JSON), or ""

Categories: keyword, string, comment, number, function, type, property, constant, operator, tag, meta."""

from __future__ import annotations

import html as _html
import json
import re
from functools import lru_cache
from pathlib import Path

MAX_CHARS = 60_000                      # longer text is shown plain: highlighting is for reading, not for megabytes

EXT_LANG = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascript", ".ts": "typescript", ".tsx": "tsx", ".json": "json", ".jsonc": "json", ".yaml": "yaml",
    ".yml": "yaml", ".toml": "toml", ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss", ".md": "markdown",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp", ".rb": "ruby", ".php": "php",
    ".lua": "lua", ".sql": "sql", ".xml": "xml", ".swift": "swift", ".ps1": "powershell", ".dockerfile": "dockerfile",
    ".ini": "ini", ".cfg": "ini", ".gradle": "groovy", ".scala": "scala", ".dart": "dart", ".r": "r", ".ex": "elixir",
}
NAME_LANG = {"dockerfile": "dockerfile", "makefile": "make", "pom.xml": "xml", ".bashrc": "bash", ".zshrc": "bash"}
PYGMENTS_NAME = {"javascript": "javascript", "typescript": "typescript", "tsx": "tsx", "csharp": "csharp",
                 "cpp": "cpp", "bash": "bash", "powershell": "powershell", "make": "make", "dockerfile": "docker"}


def lang_for(path_or_text: str = "", text: str = "") -> str:
    """A language from a file name; with `text` and no usable name, JSON is recognised by parsing it."""
    name = Path(str(path_or_text)).name.lower() if path_or_text else ""
    if name in NAME_LANG:
        return NAME_LANG[name]
    ext = Path(name).suffix
    if ext in EXT_LANG:
        return EXT_LANG[ext]
    sample = (text or "").lstrip()
    if sample[:1] in ("{", "[") and len(sample) < MAX_CHARS:
        try:
            json.loads(sample)
            return "json"
        except ValueError:
            pass
    return ""


# ---------------------------------------------------------------- tree-sitter

_CONSTANTS = {"true", "false", "none", "null", "nil", "undefined", "True", "False", "None", "NaN"}
_DEF_PARENTS = ("function", "method", "def", "func", "fn", "constructor")
_TYPE_PARENTS = ("class", "struct", "interface", "enum", "trait", "type_alias", "type_declaration", "impl")


def _ts_parser(lang: str):
    try:
        from tree_sitter_language_pack import get_parser
        return get_parser(lang)
    except Exception:  # noqa: BLE001 - extra not installed, or a grammar missing from the pack
        return None


def _is_key(node) -> bool:
    """Is this scalar the key of a `key: value` pair (JSON, YAML, TOML)? Looks a few levels up the tree."""
    cur = node
    for _ in range(4):
        up = cur.parent
        if up is None:
            return False
        if up.type.endswith("pair") or up.type in ("pair", "table_array_element"):
            key = up.child_by_field_name("key")
            return key is not None and key.id == cur.id
        cur = up
    return False


def _ts_spans(text: str, lang: str):
    parser = _ts_parser(lang)
    if parser is None:
        return None
    src = text.encode("utf-8")
    try:
        tree = parser.parse(src)
    except Exception:  # noqa: BLE001
        return None
    out: list[tuple[int, int, str]] = []

    def span(node, cat):                                   # byte offsets → character offsets, lazily
        out.append((node.start_byte, node.end_byte, cat))

    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        t = node.type
        parent = node.parent
        if "comment" in t:
            span(node, "comment")
            continue
        if t in ("string", "string_literal", "raw_string_literal", "template_string", "interpreted_string_literal",
                 "character_literal", "char_literal", "string_content", "heredoc_body", "quoted_attribute_value",
                 "double_quoted_scalar", "single_quoted_scalar", "string_scalar", "basic_string", "literal_string") \
                or (t.startswith("string") and node.child_count == 0):
            if parent is not None and parent.type == "predefined_type":
                span(node, "type")
            else:
                span(node, "property" if _is_key(node) else "string")
            continue
        if t in ("number", "string") and parent is not None and parent.type == "predefined_type":
            span(node, "type")
            continue
        if t in ("integer", "float", "number", "integer_literal", "float_literal", "int_literal", "number_literal",
                 "decimal_integer_literal", "decimal_floating_point_literal", "integer_scalar", "float_scalar"):
            span(node, "number")
            continue
        if node.child_count == 0:
            word = src[node.start_byte:node.end_byte].decode("utf-8", "replace")
            if not node.is_named and re.fullmatch(r"[A-Za-z_]+", t or ""):
                span(node, "constant" if word in _CONSTANTS else "keyword")
            elif t in ("true", "false", "none", "null", "nil", "boolean") or (node.is_named and word in _CONSTANTS):
                span(node, "constant")
            elif node.is_named and "identifier" in t and parent is not None:
                field = None
                try:
                    named = parent.child_by_field_name("name")
                    field = named is not None and named.id == node.id
                except Exception:  # noqa: BLE001
                    field = False
                if field and any(k in parent.type for k in _TYPE_PARENTS):
                    span(node, "type")
                elif field and any(k in parent.type for k in _DEF_PARENTS):
                    span(node, "function")
                elif "type" in t:
                    span(node, "type")
            elif t in ("bare_key", "quoted_key", "property_identifier", "shorthand_property_identifier"):
                span(node, "property")
            elif t in ("tag_name", "element_name"):
                span(node, "tag")
            elif t in ("attribute_name", "property_name", "field_identifier") and "key" in (parent.type if parent else ""):
                span(node, "property")
            continue
        stack.extend(reversed(node.children))
    return _to_chars(text, src, out)


def _to_chars(text: str, src: bytes, spans):
    """Byte spans → character spans (identical for ASCII; mapped through a prefix table otherwise)."""
    if len(src) == len(text):
        return sorted(_clean(spans))
    table, pos = {}, 0
    for i, ch in enumerate(text):
        table[pos] = i
        pos += len(ch.encode("utf-8"))
    table[pos] = len(text)
    fixed = [(table[a], table[b], c) for a, b, c in spans if a in table and b in table]
    return sorted(_clean(fixed))


def _clean(spans):
    last = -1
    for a, b, c in sorted(spans):
        if a >= last and b > a:
            last = b
            yield a, b, c


# ---------------------------------------------------------------- pygments fallback

def _token_category(tok) -> str:
    from pygments.token import Token
    if tok in Token.Comment:
        return "comment"
    if tok in Token.Literal.String:
        return "string"
    if tok in Token.Literal.Number:
        return "number"
    if tok in Token.Name.Function:
        return "function"
    if tok in Token.Name.Class or tok in Token.Name.Namespace or tok in Token.Keyword.Type:
        return "type"
    if tok in Token.Name.Tag:
        return "tag"
    if tok in Token.Name.Attribute or tok in Token.Name.Property:
        return "property"
    if tok in Token.Name.Builtin or tok in Token.Keyword.Constant:
        return "constant"
    if tok in Token.Keyword:
        return "keyword"
    if tok in Token.Operator.Word:
        return "keyword"
    if tok in Token.Comment.Preproc or tok in Token.Name.Decorator:
        return "meta"
    return ""


def _pygments_spans(text: str, lang: str):
    try:
        from pygments.lexers import get_lexer_by_name
        lexer = get_lexer_by_name(PYGMENTS_NAME.get(lang, lang))
    except Exception:  # noqa: BLE001 - no lexer for this language
        return None
    out, pos = [], 0
    for tok, value in lexer.get_tokens(text):
        cat = _token_category(tok)
        if cat and value.strip():
            lead = len(value) - len(value.lstrip())
            out.append((pos + lead, pos + len(value.rstrip()), cat))
        pos += len(value)
    merged: list[list] = []
    for a, b, c in sorted(_clean(out)):              # Pygments splits a string into quote / body / quote: rejoin
        if merged and merged[-1][2] == c and merged[-1][1] == a:
            merged[-1][1] = b
        else:
            merged.append([a, b, c])
    return [tuple(m) for m in merged]


@lru_cache(maxsize=64)
def _spans_cached(text: str, lang: str):
    found = _ts_spans(text, lang)
    if found is None:
        found = _pygments_spans(text, lang)
    return tuple(found or ())


def spans(text: str, lang: str) -> list[tuple[int, int, str]]:
    """Highlight spans for `text` in `lang`; [] when there is no language, no lexer or the text is huge."""
    if not lang or not text or len(text) > MAX_CHARS:
        return []
    try:
        return list(_spans_cached(text, lang))
    except Exception:  # noqa: BLE001 - highlighting must never break showing the text
        return []


def engine(lang: str = "python") -> str:
    """Which engine highlights `lang` right now: 'tree-sitter', 'pygments' or 'none' (for tests and /doctor)."""
    if _ts_parser(lang) is not None:
        return "tree-sitter"
    return "pygments" if _pygments_spans("x", lang) is not None else "none"


# ---------------------------------------------------------------- output formats

THEME = {  # one palette for the terminal (Rich styles) — the web UI has the same names in style.css
    "keyword": "bold #c792ea", "string": "#c3e88d", "comment": "italic #6b7394", "number": "#f78c6c",
    "function": "#82aaff", "type": "#ffcb6b", "property": "#89ddff", "constant": "#f78c6c", "operator": "#89ddff",
    "tag": "#f07178", "meta": "#c792ea",
}


def rich_markup(text: str, lang: str) -> str:
    """Rich console markup: brackets escaped, spans wrapped in [style]…[/]."""
    def esc(s: str) -> str:
        return s.replace("[", "\\[")
    parts, pos = [], 0
    for a, b, cat in spans(text, lang):
        parts.append(esc(text[pos:a]))
        parts.append(f"[{THEME[cat]}]{esc(text[a:b])}[/]")
        pos = b
    parts.append(esc(text[pos:]))
    return "".join(parts)


def html(text: str, lang: str) -> str:
    """Escaped HTML with `<span class="tok-keyword">` etc.; safe to assign to innerHTML."""
    parts, pos = [], 0
    for a, b, cat in spans(text, lang):
        parts.append(_html.escape(text[pos:a], quote=False))
        parts.append(f'<span class="tok-{cat}">{_html.escape(text[a:b], quote=False)}</span>')
        pos = b
    parts.append(_html.escape(text[pos:], quote=False))
    return "".join(parts)


def _per_line(text: str, lang: str):
    """[(line, [(a, b, category)])] with span offsets local to each line (a multi-line string is cut at the breaks)."""
    sp = spans(text, lang)
    out, pos, i = [], 0, 0
    for line in text.split("\n"):
        end = pos + len(line)
        local = []
        while i < len(sp) and sp[i][0] < end + 1:
            a, b, c = sp[i]
            local.append((max(a, pos) - pos, min(b, end) - pos, c))
            if b <= end + 1:
                i += 1
            else:
                break
        out.append((line, [x for x in local if x[1] > x[0]]))
        pos = end + 1
    return out


def html_lines(text: str, lang: str) -> list[str]:
    """One HTML string per line (spans never cross a line, so a line can be shown on its own)."""
    out = []
    for line, sp in _per_line(text, lang):
        parts, pos = [], 0
        for a, b, cat in sp:
            parts.append(_html.escape(line[pos:a], quote=False))
            parts.append(f'<span class="tok-{cat}">{_html.escape(line[a:b], quote=False)}</span>')
            pos = b
        parts.append(_html.escape(line[pos:], quote=False))
        out.append("".join(parts))
    return out


def rich_lines(text: str, lang: str) -> list[str]:
    """One Rich-markup string per line."""
    out = []
    for line, sp in _per_line(text, lang):
        parts, pos = [], 0
        for a, b, cat in sp:
            parts.append(line[pos:a].replace("[", "\\["))
            parts.append(f"[{THEME[cat]}]" + line[a:b].replace("[", "\\[") + "[/]")
            pos = b
        parts.append(line[pos:].replace("[", "\\["))
        out.append("".join(parts))
    return out


_NUMBERED = re.compile(r"^(\s*\d+)(\t|  )(.*)$")


def strip_line_numbers(text: str) -> tuple[list[str], list[str]]:
    """The `read` tool prefixes every line with its number: (prefixes, code lines), so the code can be highlighted
    as a whole and the numbers put back. ([], lines) when the text is not numbered."""
    lines = text.splitlines()
    pre, code = [], []
    for ln in lines:
        m = _NUMBERED.match(ln)
        if not m:
            return [], lines
        pre.append(m.group(1) + m.group(2))
        code.append(m.group(3))
    return pre, code
