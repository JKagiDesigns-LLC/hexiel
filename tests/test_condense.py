"""Local output condensing: repeated lines collapse, errors survive, known commands become digests."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import condense


def test_short_output_is_untouched():
    assert condense.condense("hello\nworld") == "hello\nworld"


def test_progress_noise_collapses_and_errors_survive():
    lines = ["starting build"] + [f"Downloading chunk {i} of 5000 (0x{i:08x})" for i in range(5000)]
    lines += ["error: linker failed: undefined reference to `foo'"] + [f"step {i} ok" for i in range(3)] + ["done"]
    out = condense.condense("\n".join(lines))
    assert "× 4998 similar lines removed" in out
    assert "undefined reference to `foo'" in out and out.endswith("]") and "done" in out
    assert len(out) < 2000


def test_unique_long_log_keeps_head_tail_and_every_error():
    lines = [f"line {i} payload {'x' * (i % 7)} {chr(97 + i % 26) * 30}" for i in range(4000)]
    lines[1234] = "Traceback (most recent call last): boom"
    lines[2345] = "WARNING: disk almost full"
    out = condense.condense("\n".join(lines), limit=8000)
    assert "boom" in out and "disk almost full" in out
    assert lines[0] in out and lines[-1] in out
    assert "lines removed" in out and "error/warning lines kept" in out


def test_condense_is_fast_on_megabytes():
    text = "\n".join(f"{i} some log line with id {i * 7919} at 12:00:{i % 60:02d}" for i in range(200_000))
    t = time.time()
    condense.condense(text)
    assert time.time() - t < 3.0


def test_git_status_digest():
    out = ("On branch main\nYour branch is ahead of 'origin/main' by 2 commits.\n\n"
           "Changes to be committed:\n  (use \"git restore --staged <file>...\" to unstage)\n"
           "\tnew file:   a.py\n\tmodified:   b.py\n\n"
           "Changes not staged for commit:\n" + "".join(f"\tmodified:   src/file_{i}.py\n" for i in range(60)) +
           "\nUntracked files:\n  (use \"git add <file>...\" to include in what will be committed)\n\tnotes.txt\n")
    digest = condense.structure("git status", out)
    assert digest.startswith("branch main, ahead of 'origin/main' by 2 commits")
    assert "staged (2): n a.py, b.py" in digest and "modified (60)" in digest and "… 20 more" in digest
    assert "untracked (1): notes.txt" in digest and len(digest) < len(out) / 2


def test_pip_list_digest_and_unknown_commands():
    out = "Package    Version\n---------- -------\n" + "\n".join(f"pkg{i}      1.{i}.0" for i in range(80))
    digest = condense.structure("pip list", out)
    assert digest.startswith("80 packages: pkg0 1.0.0, pkg1 1.1.0") and len(digest) < len(out)
    assert condense.structure("make all", out) is None
    assert condense.structure("pip list", "a 1\n") is None
