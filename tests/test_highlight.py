"""Syntax colours for the terminal and web views: tree-sitter when installed, Pygments otherwise, never a crash."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import highlight as h


def cats(text, lang):
    return [(text[a:b], c) for a, b, c in h.spans(text, lang)]


def test_python_tokens():
    got = dict(cats('def add(a):\n    return a + 1  # sum\n', "python"))
    assert got["def"] == "keyword" and got["add"] == "function" and got["1"] == "number" and got["# sum"] == "comment"


def test_json_keys_are_not_values():
    got = cats('{"name": "hexiel", "n": 3, "ok": true}', "json")
    assert ('"name"', "property") in got and ('"hexiel"', "string") in got and ("3", "number") in got and ("true", "constant") in got


def test_unicode_offsets_stay_aligned():
    text = 'x = "é—ü"  # ünï\n'
    got = cats(text, "python")
    assert ('"é—ü"', "string") in got and ("# ünï", "comment") in got


def test_unknown_language_and_huge_text_are_plain():
    assert h.spans("hello", "") == [] and h.spans("hello", "no-such-lang") == []
    assert h.spans("x = 1\n" * 20000, "python") == []


def test_html_is_escaped_and_spans_wrap_tokens():
    out = h.html('x = "<script>alert(1)</script>"', "python")
    assert "<script>" not in out and "&lt;script&gt;" in out and 'class="tok-string"' in out


def test_rich_markup_escapes_brackets():
    out = h.rich_markup('d["k"] = [1]', "python")
    assert "\\[" in out and "[#c3e88d]" in out


def test_language_from_a_name_or_from_json_text():
    assert h.lang_for("src/app.ts") == "typescript" and h.lang_for("Dockerfile") == "dockerfile"
    assert h.lang_for("", '{"a": 1}') == "json" and h.lang_for("notes.txt", "hello") == ""


def test_numbered_read_output_is_split_back_into_code():
    pre, code = h.strip_line_numbers("     1  import os\n     2  x = 1\n")
    assert code == ["import os", "x = 1"] and pre[0].strip() == "1"
    assert h.strip_line_numbers("plain text\nmore") == ([], ["plain text", "more"])


def test_pygments_fallback_when_tree_sitter_is_missing(monkeypatch):
    monkeypatch.setattr(h, "_ts_parser", lambda lang: None)
    h._spans_cached.cache_clear()
    try:
        got = cats("def f():\n    return 'x'  # c\n", "python")
        assert ("def", "keyword") in got and ("# c", "comment") in got and ("'x'", "string") in got
        assert h.engine("python") == "pygments"
    finally:
        h._spans_cached.cache_clear()


@pytest.mark.skipif(h.engine("python") != "tree-sitter", reason="hexiel[code] not installed")
def test_tree_sitter_engine_is_used_when_present():
    assert h.engine("json") == "tree-sitter"


def test_lines_are_highlighted_one_by_one_even_inside_multiline_strings():
    code = 'x = """one\ntwo"""\ny = 2'
    lines = h.html_lines(code, "python")
    assert len(lines) == 3 and lines[0].count("tok-string") == 1 and 'tok-string">two' in lines[1] and "tok-number" in lines[2]
    rich = h.rich_lines('d["k"]', "python")
    assert rich[0].startswith("d\\[")


def test_preview_read_keeps_numbers_and_colours_code():
    from hexiel import highlight
    call = {"name": "read", "arguments": {"path": "x.py"}}
    out = highlight.preview(call, "     1\tdef f(a):\n     2\t    return 'hi'\n", "rich")
    assert out and out[0].startswith("[dim]     1\t[/]") and "[" in out[0][12:] and "def" in out[0]
    html = highlight.preview(call, "     1\tdef f(a):\n", "html")
    assert html and "tok-keyword" in html[0] and "tok-meta" in html[0]


def test_preview_json_result_and_plain_text_skipped():
    from hexiel import highlight
    assert highlight.preview({"name": "http"}, '{"a": 1, "b": [true]}', "html")
    assert highlight.preview({"name": "bash"}, "hello world\nfine", "html") is None


def test_preview_handles_read_path_header():
    from hexiel import highlight
    out = highlight.preview({"name": "read", "arguments": {"path": "a.py"}}, "/p/a.py\n     1\tx = 1\n", "html")
    assert out[0].count("/p/a.py") == 1 and "tok-meta" in out[1] and "tok-number" in out[1].split("\t")[-1]
