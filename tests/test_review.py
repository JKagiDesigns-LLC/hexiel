"""review pack: facts about an edit for the human who reviews it."""
import subprocess

import pytest

from hexiel import review
from hexiel import symbols as sm


def git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *a], check=True,
                   capture_output=True)


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.delenv("GIT_INDEX_FILE", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "core.py").write_text(
        "def price(x):\n    return x * 2\n\n\ndef old():\n    return 0\n\n\ndef steady():\n    return 1\n")
    (tmp_path / "pkg" / "api.py").write_text(
        "from pkg.core import price, old\n\n\ndef quote(x):\n    return price(x) + old()\n")
    (tmp_path / "tests" / "test_api.py").write_text(
        "from pkg.api import quote\n\n\ndef test_quote():\n    assert quote(1) == 2\n")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "init")
    sm._INDEXES.clear()
    return tmp_path


def pack(p):
    return review.build(str(p), "HEAD", None, with_checks=False)


def test_nothing_to_review(proj):
    assert "nothing to review" in pack(proj)


def test_an_interface_change_is_the_first_thing_to_look_at(proj):
    (proj / "pkg" / "core.py").write_text(
        "def price(x, rate):\n    return x * rate\n\n\ndef steady():\n    return 1\n\n\ndef fresh():\n    return 2\n")
    text = pack(proj)
    assert text.startswith("# Review pack  (1 file(s)")
    assert "## Look at first: the interface changed" in text
    line = next(l for l in text.splitlines() if l.startswith("- `price`"))
    assert "(x)" in line and "→" in line and "(x, rate)" in line
    assert "`old` removed" in text and "reference(s) elsewhere" in text
    assert "## New public code" in text and "`fresh`" in text
    assert "no test file changed in this diff" in text            # source changed, tests untouched
    assert "Run the affected tests: `pytest tests/test_api.py`" in text


def test_changed_code_no_test_mentions_is_called_out(proj):
    (proj / "pkg" / "core.py").write_text(
        "def price(x):\n    return x * 2\n\n\ndef old():\n    return 0\n\n\ndef steady():\n    return 99\n")
    text = pack(proj)
    assert "## Unverified" in text and "no test file references `steady`" in text
    assert "price" not in text.split("## Changed code")[1]          # untouched code is not listed


def test_test_only_and_doc_changes_are_grouped_and_not_flagged(proj):
    (proj / "README.md").write_text("hello\n")
    (proj / "tests" / "test_api.py").write_text(
        "from pkg.api import quote\n\n\ndef test_quote():\n    assert quote(1) == 2\n\n\ndef test_more():\n    pass\n")
    git(proj, "add", "-N", "README.md")
    text = pack(proj)
    assert "**tests**: tests/test_api.py" in text and "**docs**: README.md" in text
    assert "Unverified" not in text and "Look at first" not in text


def test_kinds():
    assert review.kind_of("tests/test_x.py") == "tests" and review.kind_of("a/b.md") == "docs"
    assert review.kind_of("pyproject.toml") == "config" and review.kind_of("src/x.go") == "source"


def test_base_other_than_head(proj):
    (proj / "pkg" / "core.py").write_text("def price(x):\n    return 7\n\n\ndef old():\n    return 0\n\n\ndef steady():\n    return 1\n")
    git(proj, "commit", "-qam", "change")
    sm._INDEXES.clear()
    assert "nothing to review" in pack(proj)
    assert "against HEAD~1" in review.build(str(proj), "HEAD~1", None, with_checks=False)


def test_tool_and_cli_flag_exist(monkeypatch):
    from hexiel.main import parse_args
    from hexiel.tools import Toolchain, build_tools
    assert "review_pack" in {t.name for t in build_tools(Toolchain())}
    monkeypatch.setattr("sys.argv", ["hexiel", "--review", "main"])
    assert parse_args().review == "main"
