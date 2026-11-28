"""blast_radius: who is affected by a change, which tests touch it, and what the working-tree diff really touches."""
import subprocess

import pytest

from hexiel import impact
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
        "def price(x):\n    return x * 2\n\n\ndef unused():\n    return 0\n\n\ndef _private():\n    return 1\n")
    (tmp_path / "pkg" / "api.py").write_text(
        "from pkg.core import price\n\n\ndef quote(x):\n    return price(x) + 1\n\n\ndef report(x):\n    return quote(x)\n")
    (tmp_path / "tests" / "test_api.py").write_text(
        "from pkg.api import quote\n\n\ndef test_quote():\n    assert quote(1) == 3\n")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "init")
    sm._INDEXES.clear()
    return tmp_path


def test_which_files_are_tests():
    assert impact.is_test_file("tests/test_api.py") and impact.is_test_file("src/a.test.ts")
    assert impact.is_test_file("pkg/foo_test.go") and impact.is_test_file("e2e/flow.sh")
    assert not impact.is_test_file("pkg/core.py") and not impact.is_test_file("src/latest.py")


def test_radius_follows_two_hops_and_finds_the_tests(proj):
    text, err = impact.run({"target": "price"}, str(proj))
    assert not err
    assert "function price  pkg/core.py:1-2" in text
    assert "pkg/api.py:5  (in quote)" in text                  # direct caller, with who it sits in
    assert "pkg/api.py:9  (in report, calls quote)" in text    # second hop
    assert "tests/test_api.py" in text                          # reached through quote
    assert "run only those: pytest tests/test_api.py" in text


def test_nothing_else_is_affected_says_so(proj):
    text, _ = impact.run({"target": "unused"}, str(proj))
    assert "nothing else is affected" in text


def test_untested_code_is_called_out(proj):
    text, _ = impact.run({"target": "report"}, str(proj))
    assert "no references" in text or "no test file references it" in text


def test_a_file_target_lists_its_public_symbols_only(proj):
    text, _ = impact.run({"target": "pkg/core.py"}, str(proj))
    assert "## price" in text and "## unused" in text and "_private" not in text


def test_changes_maps_the_diff_to_the_symbols_it_touches(proj):
    (proj / "pkg" / "core.py").write_text(
        "def price(x):\n    return x * 3\n\n\ndef unused():\n    return 0\n\n\ndef _private():\n    return 1\n")
    text, err = impact.run({"target": "changes"}, str(proj))
    assert not err and "## price (pkg/core.py:1)" in text and "unused" not in text
    assert "run the affected tests: pytest tests/test_api.py" in text
    assert impact.changed_ranges(str(proj)) == {"pkg/core.py": [(2, 2)]}


def test_changes_with_a_clean_tree_and_with_non_code(proj):
    assert "no uncommitted changes" in impact.run({"target": "changes"}, str(proj))[0]
    (proj / "README.md").write_text("hi\n")
    git(proj, "add", "-A")
    text, _ = impact.run({"target": "changes"}, str(proj))
    assert "README.md" not in text or "no function or class" in text or "no uncommitted" in text


def test_bad_input_is_a_readable_error(proj):
    assert impact.run({}, str(proj))[1]
    assert "no definition found" in impact.run({"target": "nope_nothing"}, str(proj))[0]


def test_test_command_per_runner():
    class R:
        def __init__(self, label): self.label = label
    class C:
        test_runners = [R("vitest")]
    assert impact.test_command(C(), ["web/a.test.ts"]) == "npx vitest run web/a.test.ts"
    assert impact.test_command(None, ["pkg/a_test.go"]) == "go test ./pkg"
    assert impact.test_command(None, []) == ""
    assert impact.test_command(None, ["x.rb"]) == ""      # unknown runner: no made-up command


def test_tool_is_registered_and_readonly():
    from hexiel.tools import Toolchain, build_tools
    t = {t.name: t for t in build_tools(Toolchain())}["blast_radius"]
    assert t.readonly
