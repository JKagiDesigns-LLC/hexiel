"""#39 formatter + AST-aware editing — format-on-write through the real ruff, the
format=false escape hatch, settings toggles, and whitespace-equivalent edit anchors."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hexiel import formatters as F
from hexiel.tools import Toolchain, ToolContext, build_tools


def make_ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(cwd=str(tmp_path), chain=Toolchain())


def _py(tmp_path: Path, name="x.py") -> Path:
    p = tmp_path / name
    if not p.exists():
        p.write_text("")
    return p


# ---------------------------------------------------------------- anchor recovery

def test_fuzzy_span_recovers_whitespace_drift():
    text = "def a():\n\treturn 1\n\ndef b():\n\t\t\treturn  2\n"
    span = F.fuzzy_span(text, "def b():\n    return 2")
    assert span is not None
    start, end = span
    assert "def b():" in text[start:end] and "return" in text[start:end]
    # trailing whitespace differences never matter
    assert F.fuzzy_span("if ready:\n    go()\n", "if ready:\n    go()   \n") is not None


def test_fuzzy_span_refuses_ambiguous_and_absent():
    assert F.fuzzy_span("if x:\n    a()\nelse:\n    a()\n", "a()") is None     # two hits
    assert F.fuzzy_span("nothing here\n", "missing") is None                   # absent
    assert F.fuzzy_span("x = 1\n", "") is None                                  # empty anchor


def test_fuzzy_replace_lands_in_place(tmp_path):
    from hexiel import tools as tools_mod
    p = _py(tmp_path, "drift.py")
    p.write_text("def a():\n\treturn 1\n\n\ndef b():\n\treturn 2\n")
    ctx = make_ctx(tmp_path)
    ctx.read_files.add(str(p))                        # pretend a read happened (skip the stale guard)
    out = tools_mod._edit({"path": str(p), "old_string": "def b():\n    return 2",
                           "new_string": "def b():\n    return 3"}, ctx)
    assert "edited" in out.text and "(whitespace-equivalent match)" in out.text
    assert "return 3" in p.read_text() and "return 1" in p.read_text()


def test_exact_match_still_preferred_and_unique(tmp_path):
    tools = {t.name: t for t in build_tools(Toolchain())}
    p = _py(tmp_path, "same.py")
    p.write_text("a = 1\nb = 2\n")
    out = tools["edit"].run({"path": str(p), "old_string": "b = 2", "new_string": "b = 9"},
                            make_ctx(tmp_path))
    assert "edited" in out.text and "(whitespace-equivalent match)" not in out.text
    assert "b = 9" in p.read_text()
    out = tools["edit"].run({"path": str(p), "old_string": "missing text", "new_string": "x"},
                            make_ctx(tmp_path))
    assert out.is_error and "whitespace-equivalent tried" in out.text


# ---------------------------------------------------------------- format-on-write

def test_format_on_write_real_ruff(tmp_path, monkeypatch):
    """ruff IS installed in the dev venv: the write shows the note and the content is normalized."""
    if not __import__("shutil").which("ruff"):
        import shutil as sh
        monkeypatch.setattr("hexiel.formatters.shutil.which", lambda n: "/usr/bin/ruff" if n == "ruff" else None)
    tools = {t.name: t for t in build_tools(Toolchain())}
    p = _py(tmp_path, "fmtcheck.py")
    out = tools["write"].run({"path": str(p), "content": "import os,sys\nx=1\n"}, make_ctx(tmp_path))
    assert "formatted with ruff" in out.text
    assert "import os, sys" in p.read_text() and "x = 1" in p.read_text()
    # format=false leaves it alone
    out = tools["write"].run({"path": str(p), "content": "y=2\n", "format": False}, make_ctx(tmp_path))
    assert "formatted" not in out.text
    assert "y=2" in p.read_text()


def test_formatter_off_in_config(tmp_path, monkeypatch):
    from hexiel import config as config_mod
    from hexiel import tools as tools_mod
    from hexiel.config import ModelConfig, Settings
    from hexiel.session import Session
    tools = {t.name: t for t in build_tools(Toolchain())}
    p = _py(tmp_path, "off.py")
    config_mod.CONFIG_FILE.write_text('settings.formatter = "off"\n'
                                      '[models.m]\nprovider = "openai"\nmodel = "m"\nbase_url = ""\n')
    try:
        out = tools["write"].run({"path": str(p),
                                  "content": "import os,sys\nx=1\n"}, make_ctx(tmp_path))
        assert "formatted with" not in out.text
        assert "import os,sys" in p.read_text()
    finally:
        config_mod.CONFIG_FILE.unlink(missing_ok=True)


def test_formatter_failure_never_fails_the_edit(tmp_path, monkeypatch):
    from hexiel import formatters
    p = _py(tmp_path, "boom.py")
    p.write_text("z = 0\n")

    def explode(*a, **k):
        raise TimeoutError("slow ruff")

    monkeypatch.setattr("hexiel.formatters.shutil.which", lambda n: "/usr/bin/ruff" if n == "ruff" else None)
    monkeypatch.setattr(formatters.subprocess, "run", explode)
    got = formatters.format_file(p, timeout=0.1)
    assert got == ""                                   # the write already happened; no crash, no weird note
    assert p.read_text() == "z = 0\n"


def test_settings_editor_lists_format_toggle():
    from hexiel import settings_edit
    field = next(f for f in settings_edit.FIELDS if f["key"] == "settings.formatter")
    assert field["options"] == ["auto", "off"]
