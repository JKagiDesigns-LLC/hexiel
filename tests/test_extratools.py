"""Extra toolsets: desktop (screenshot / clipboard / ocr) and the .ipynb notebook tool."""

import json
import shutil
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from hexiel import datatools as D
from hexiel import desktop as DT
from hexiel import toolsets as TS
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext


def ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path), chain=Toolchain())


# ---------------------------------------------------------------- notebook (.ipynb)

def write_nb(tmp_path, extra=None):
    cells = [
        {"cell_type": "code", "metadata": {}, "execution_count": 1,
         "outputs": [{"output_type": "stream", "name": "stdout", "text": "hi\n"}],
         "source": "print('hi')"},
        {"cell_type": "markdown", "metadata": {}, "source": "# Title"},
    ]
    if extra:
        cells.extend(extra)
    p = tmp_path / "nb.ipynb"
    p.write_text(json.dumps({"cells": cells, "metadata": {}, "nbformat": 4, "nbformat_minor": 5}))
    return p


def test_cells_lists_every_cell(tmp_path):
    p = write_nb(tmp_path)
    out = D._notebook({"action": "cells", "path": "nb.ipynb"}, ctx(tmp_path)).text
    assert "2 cells" in out
    assert "[0] code · output: print('hi')" in out
    assert "[1] markdown" in out and "# Title" in out


def test_replace_clears_outputs(tmp_path):
    p = write_nb(tmp_path)
    r = D._notebook({"action": "replace", "path": "nb.ipynb", "index": 0,
                     "source": "print('bye')"}, ctx(tmp_path))
    assert not r.is_error, r.text
    data = json.loads(p.read_text())
    cell = data["cells"][0]
    assert "".join(cell["source"]) == "print('bye')" and cell["outputs"] == [] and cell["execution_count"] is None


def test_replace_can_retype_a_cell(tmp_path):
    p = write_nb(tmp_path)
    D._notebook({"action": "replace", "path": "nb.ipynb", "index": 0, "kind": "markdown",
                 "source": "# now prose"}, ctx(tmp_path))
    cell = json.loads(p.read_text())["cells"][0]
    assert cell["cell_type"] == "markdown" and "".join(cell["source"]) == "# now prose"


def test_insert_appends_with_index_minus_one(tmp_path):
    p = write_nb(tmp_path)
    D._notebook({"action": "insert", "path": "nb.ipynb", "index": -1, "kind": "markdown",
                 "source": "## Foot"}, ctx(tmp_path))
    cells = json.loads(p.read_text())["cells"]
    assert len(cells) == 3 and cells[2]["cell_type"] == "markdown"
    assert "## Foot" in "".join(cells[2]["source"])


def test_insert_code_gets_empty_outputs(tmp_path):
    p = write_nb(tmp_path)
    D._notebook({"action": "insert", "path": "nb.ipynb", "index": 0, "source": "x = 1"}, ctx(tmp_path))
    cell = json.loads(p.read_text())["cells"][1]
    assert cell["cell_type"] == "code" and cell["outputs"] == [] and cell["execution_count"] is None


def test_delete_and_clear_output(tmp_path):
    p = write_nb(tmp_path)
    assert "deleted cell [0] (code)" in D._notebook(
        {"action": "delete", "path": "nb.ipynb", "index": 0}, ctx(tmp_path)).text
    assert "cleared outputs" in D._notebook(
        {"action": "clear_output", "path": "nb.ipynb", "index": 0}, ctx(tmp_path)).text
    cells = json.loads(p.read_text())["cells"]
    assert cells[0]["execution_count"] is None and cells[0]["outputs"] == []


def test_notebook_errors(tmp_path):
    p = write_nb(tmp_path)
    c = ctx(tmp_path)
    assert "out of range" in D._notebook({"action": "delete", "path": "nb.ipynb", "index": 99}, c).text
    assert "source" in D._notebook({"action": "insert", "path": "nb.ipynb", "index": -1}, c).text
    assert "error" in D._notebook({"action": "cells", "path": "nope.ipynb"}, c).text
    (tmp_path / "broken.ipynb").write_text("not json")
    assert "error" in D._notebook({"action": "cells", "path": "broken.ipynb"}, c).text


def test_notebook_cells_also_note_the_file(tmp_path):
    """cells is a read too: a user edit afterwards makes the next mutation refuse (stale guard)."""
    p = write_nb(tmp_path)
    c = ctx(tmp_path)
    D._notebook({"action": "cells", "path": "nb.ipynb"}, c)
    p.write_text(p.read_text().replace("# Title", "# Changed"))
    r = D._notebook({"action": "replace", "path": "nb.ipynb", "index": 1, "source": "x"}, c)
    assert r.is_error and "changed on disk" in r.text



    p = write_nb(tmp_path)
    c = ctx(tmp_path)
    c.note_file(str(p))
    p.write_text(json.dumps({"cells": [], "metadata": {}, "nbformat": 4}))   # someone else rewrites it
    r = D._notebook({"action": "delete", "path": "nb.ipynb", "index": 0}, c)
    assert r.is_error and "changed on disk" in r.text


# ---------------------------------------------------------------- desktop toolset

def tiny_png() -> bytes:
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + chunk(b"IEND", b"")


# ---------------------------------------------------------------- desktop toolset

def put_on_path(tmp_path, monkeypatch, name, script):
    d = tmp_path / "fakebin"
    d.mkdir(exist_ok=True)
    f = d / name
    f.write_text("#!/bin/sh\n" + script)
    f.chmod(0o755)
    monkeypatch.setenv("PATH", str(d) + ":/usr/bin:/bin")
    return f


def test_screenshot_capture(tmp_path, monkeypatch):
    png = tmp_path / "shot.png"
    png.write_bytes(tiny_png())
    put_on_path(tmp_path, monkeypatch, "grim", f'cp "{png}" "$1"')
    r = DT._screenshot({}, ctx(tmp_path))
    assert not r.is_error and r.images, r.text
    saved = Path(home_screenshots()).iterdir()
    assert any(f.stat().st_size == len(tiny_png()) for f in saved)


def home_screenshots():
    from hexiel.config import home_data_dir
    return home_data_dir() / "screenshots"


def test_screenshot_reports_what_is_missing(tmp_path, monkeypatch):
    put_on_path(tmp_path, monkeypatch, "failsnip", 'exit 1')
    monkeypatch.setattr(DT, "_CAPTURES", [("x11", ["failsnip", "{out}"])])
    r = DT._screenshot({}, ctx(tmp_path))
    assert r.is_error and "screenshot failed" in r.text
    assert "scrot" in r.text or "grim" in r.text      # the hint names what the real platform needs


def test_screenshot_skips_dying_helpers(tmp_path, monkeypatch):
    """A helper that exits non-zero is left behind for the next one."""
    png = tmp_path / "shot.png"
    png.write_bytes(tiny_png())
    d = tmp_path / "fakebin"
    d.mkdir(exist_ok=True)
    for nm, body in (("brokenshot", "exit 1"), ("goodshot", f'cp "{png}" "$1"')):
        f = d / nm
        f.write_text("#!/bin/sh\n" + body)
        f.chmod(0o755)
    monkeypatch.setenv("PATH", str(d) + ":/usr/bin:/bin")
    monkeypatch.setattr(DT, "_CAPTURES", [("x11", ["brokenshot", "{out}"]),
                                          ("x11", ["goodshot", "{out}"])])
    r = DT._screenshot({}, ctx(tmp_path))
    assert not r.is_error and r.images, r.text


def test_clipboard_write(tmp_path, monkeypatch):
    dest = tmp_path / "clip"
    put_on_path(tmp_path, monkeypatch, "wl-copy", f'cat > "{dest}"')
    r = DT._clipboard({"op": "write", "text": "hello clipboard"}, ctx(tmp_path))
    assert not r.is_error and "copied" in r.text
    import time as T
    for _ in range(100):                          # wl-copy is a detached background process
        if dest.exists():
            break
        T.sleep(0.05)
    assert dest.read_text() == "hello clipboard"


def test_clipboard_read(tmp_path, monkeypatch):
    put_on_path(tmp_path, monkeypatch, "wl-paste", 'echo -n "the clipboard prose"')
    r = DT._clipboard({"op": "read"}, ctx(tmp_path))
    assert "the clipboard prose" in r.text


def test_clipboard_read_nothing_there(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))          # no helpers anywhere
    import hexiel.clipboard as CB
    monkeypatch.setattr(CB, "_TEXT_READERS", [["totally-missing-helper"]])
    r = DT._clipboard({"op": "read"}, ctx(tmp_path))
    assert r.is_error and "clipboard" in r.text


def test_ocr_reports_install_hint_when_tesseract_missing(tmp_path, monkeypatch):
    real = shutil.which
    monkeypatch.setattr(DT.shutil, "which", lambda n: None if n == "tesseract" else real(n))
    img = tmp_path / "x.png"
    img.write_bytes(tiny_png())
    r = DT._ocr({"path": "x.png"}, ctx(tmp_path))
    assert r.is_error and "tesseract is not installed" in r.text


def test_ocr_missing_file(tmp_path):
    r = DT._ocr({"path": "ghost.png"}, ctx(tmp_path))
    assert r.is_error and "does not exist" in r.text


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not on this machine")
def test_ocr_runs_for_real(tmp_path):
    png = tmp_path / "words.png"
    png.write_bytes(tiny_png())
    r = DT._ocr({"path": "words.png"}, ctx(tmp_path))
    assert not r.is_error          # empty page is fine: "found no text" is not an error


# ---------------------------------------------------------------- toolset wiring

def test_desktop_group_present(tmp_path, monkeypatch):
    assert "desktop" in TS.GROUPS
    assert {t.name for t in TS._group_tools("desktop")} == {"screenshot", "clipboard", "ocr"}


def test_desktop_detected_by_display(tmp_path, monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(DT.shutil, "which", lambda n: None)
    assert not TS._looks_like("desktop", str(tmp_path))
    monkeypatch.setenv("DISPLAY", ":1")
    assert TS._looks_like("desktop", str(tmp_path))


def test_ipynb_pulls_in_the_data_group(tmp_path):
    (tmp_path / "analyze.ipynb").write_text('{"cells": []}')
    assert TS._looks_like("data", str(tmp_path))


def test_notebook_in_data_group():
    assert "notebook" in {t.name for t in TS._group_tools("data")}
