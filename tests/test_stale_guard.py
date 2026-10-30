"""Stale-file guard: an edit/write to a file that changed on disk since it was read is refused
until the tool re-reads it — protecting against clobbers by the user, shell commands, or the
other hexiel window working on the same tree."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, _edit, _read, _write


def ctx_for(tmp_path):
    return ToolContext(cwd=str(tmp_path), chain=Toolchain())


def touch_ns(path: Path, later_ns: int):
    """Force a stat-visible change without waiting for the filesystem's timestamp granularity."""
    old = os.stat(path)
    os.utime(path, ns=(old.st_atime_ns + later_ns, old.st_mtime_ns + later_ns))


def test_an_external_change_between_read_and_edit_is_refused(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("one\ntwo\n")
    ctx = ctx_for(tmp_path)
    assert not _read({"path": "a.py"}, ctx).is_error
    touch_ns(f, 10**7)                                              # "another window" wrote it in between
    r = _edit({"path": "a.py", "old_string": "one", "new_string": "ONE"}, ctx)
    assert r.is_error and "changed on disk since it was read" in r.text
    assert f.read_text() == "one\ntwo\n"                            # nothing was touched


def test_rereading_clears_the_guard_and_the_edit_applies(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("one\ntwo\n")
    ctx = ctx_for(tmp_path)
    _read({"path": "a.py"}, ctx)
    touch_ns(f, 10**7)
    assert _edit({"path": "a.py", "old_string": "one", "new_string": "ONE"}, ctx).is_error
    assert not _read({"path": "a.py"}, ctx).is_error                # the re-read records the new state
    r = _edit({"path": "a.py", "old_string": "one", "new_string": "ONE"}, ctx)
    assert not r.is_error and f.read_text() == "ONE\ntwo\n"


def test_a_write_updates_the_record_for_the_next_edit(tmp_path):
    ctx = ctx_for(tmp_path)
    r = _write({"path": "new.py", "content": "x = 1\n"}, ctx)
    assert not r.is_error                                           # a brand-new file: nothing was seen before
    touch_ns(tmp_path / "new.py", 10**7)                            # changed behind hexiel's back
    r2 = _write({"path": "new.py", "content": "x = 2\n"}, ctx)
    assert r2.is_error and "changed on disk" in r2.text


def test_edits_without_a_prior_read_stay_allowed(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("keep\nme\n")
    ctx = ctx_for(tmp_path)
    r = _edit({"path": "a.py", "old_string": "me", "new_string": "you"}, ctx)
    assert not r.is_error                                           # the open policy for unseen files is unchanged
    r2 = _edit({"path": "a.py", "old_string": "keep", "new_string": "had"}, ctx)
    assert not r2.is_error, "hexiel's own write just re-noted the file: the next edit passes"