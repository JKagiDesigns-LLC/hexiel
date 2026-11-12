"""First-message project brief, the installed-programs slice per message, and `have`."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import brief, inventory
from hexiel.session import strip_env_note
from tests.test_symbols import session_in


def git_project(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "1.2.3"\n'
                                             '[project.scripts]\ndemo = "demo.cli:main"\n')
    (tmp_path / "todo.md").write_text("- [ ] ship the parser\n- [x] done thing\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t", "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "first commit"], check=True)


def test_brief_has_type_entry_points_commits_and_todos(tmp_path):
    git_project(tmp_path)
    text = brief.build(str(tmp_path))
    assert "type: Python" in text and "demo 1.2.3" in text and "`demo` → demo.cli:main" in text
    assert "first commit" in text and "1 open — ship the parser" in text and "machine:" in text
    assert len(text) <= brief.MAX_CHARS


def test_brief_skips_non_projects(tmp_path):
    assert brief.build(str(tmp_path)) == ""
    assert brief.build(str(Path.home())) == ""


def test_inventory_slice_only_when_relevant(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory, "_CACHE", {"stamp": "x", "programs": {"ffmpeg": "/usr/bin/ffmpeg"},
                                              "packages": ["duckdb"], "system": {}, "all_count": 1})
    monkeypatch.setattr(inventory, "_stamp", lambda dirs: "x")
    assert inventory.relevant("rename this variable") == ""
    vid = inventory.relevant("convert the video to mp4")
    assert "audio/video: ffmpeg" in vid
    ocr = inventory.relevant("read the text in this screenshot with OCR")
    assert "images/OCR: nothing" in ocr and "missing: magick" in ocr
    assert "python:duckdb" in inventory.relevant("query this csv")
    monkeypatch.setattr(inventory.shutil, "which", lambda n: None)
    assert inventory.have("ffmpeg").startswith("ffmpeg: installed")
    assert "not installed —" in inventory.have("definitely-not-a-real-tool")


def test_first_message_gets_the_brief_later_ones_do_not(tmp_path):
    git_project(tmp_path)
    s = session_in(tmp_path)
    first = {"role": "user", "content": "fix the parser"}
    s.messages.append(first)
    s._context_note(first, "fix the parser")
    assert "[project brief, gathered locally" in first["content"]
    assert strip_env_note(first["content"]) == "fix the parser"            # the UI shows only the person's words
    second = {"role": "user", "content": "and the tests"}
    s.messages.append(second)
    s._context_note(second, "and the tests")
    assert "project brief" not in second["content"]
    s.settings.auto_context = "off"
    third = {"role": "user", "content": "convert the video"}
    s._context_note(third, "convert the video")
    assert third["content"] == "convert the video"
