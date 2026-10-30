"""Durable turn checkpoints: per-turn snapshots on private refs (refs/hexiel/checkpoints/...) so
/undo survives a process restart. Real git plumbing against a throwaway repo."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import checkpoints as ckpt


def run(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def repo_for(tmp_path: Path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    run(tmp_path, "config", "user.email", "test@example.com")
    run(tmp_path, "config", "user.name", "tester")
    (tmp_path / "seed.txt").write_text("the seed\n")
    (tmp_path / ".gitignore").write_text("ignored/\n")
    run(tmp_path, "add", "-A")
    run(tmp_path, "commit", "-qm", "seed")
    return tmp_path


def ref_sha(repo, ref) -> str:
    return run(repo, "rev-parse", ref).stdout.strip()


def test_a_snapshot_pins_a_private_ref_and_leaves_the_users_repo_alone(tmp_path):
    repo = repo_for(tmp_path)
    (repo / "work.txt").write_text("changed by hexiel\n")            # tracked modification
    (repo / "fresh.txt").write_text("untracked, not ignored\n")      # untracked addition
    ref = ckpt.snapshot(repo, "sess1")
    assert ref and ref.startswith("refs/hexiel/checkpoints/sess1/")
    assert run(repo, "rev-parse", ref).returncode == 0
    sha = ref_sha(repo, ref)
    assert len(sha) == 40 and sha != ref_sha(repo, "HEAD")
    assert run(repo, "stash", "list").stdout.strip() == ""           # the stash list stays empty
    assert run(repo, "status", "--porcelain").stdout.count("?? ") >= 1   # the tree is untouched
    parents = run(repo, "rev-list", "--parents", "-n", "1", sha).stdout.split()
    assert len(parents) >= 2                                         # HEAD + (untracked when present) [+ stash]
    assert f"{ckpt.REF_BASE}/sess1/" in run(repo, "for-each-ref", "--format=%(refname)").stdout
    assert ckpt.snapshot(tmp_path / "not-a-repo", "s") is None       # outside a repo: no snapshot, no crash


def test_restore_last_reverts_a_tracked_change_and_the_untracked_set(tmp_path):
    repo = repo_for(tmp_path)
    (repo / "seed.txt").write_text("the seed\n")                     # ensure a clean start point
    ref = ckpt.snapshot(repo, "s2", when=1700000000)
    assert ref
    (repo / "seed.txt").write_text("edited after the snapshot\n")    # tracked: hexiel's edit
    (repo / "fresh.txt").write_text("created after\n")               # untracked file: created after
    notes, refusal = ckpt.restore_last(repo, "s2")
    assert not refusal
    assert (repo / "seed.txt").read_text() == "the seed\n"           # back to the snapshot state
    assert not (repo / "fresh.txt").exists()
    assert any("seed.txt" in n for n in notes)


def test_restore_still_removes_a_file_created_after(tmp_path):
    repo = repo_for(tmp_path)
    ckpt.snapshot(repo, "s3", when=1700000001)
    (repo / "later.txt").write_text("made after the snapshot\n")
    notes, refusal = ckpt.restore_last(repo, "s3")
    assert not refusal and not (repo / "later.txt").exists()
    assert any("removed later.txt" in n for n in notes)


def test_restore_refuses_when_head_moved(tmp_path):
    repo = repo_for(tmp_path)
    ckpt.snapshot(repo, "s4", when=1700000002)
    (repo / "seed.txt").write_text("user edit\n")
    run(repo, "add", "-A")
    run(repo, "commit", "-qm", "a real user commit")                 # HEAD moved past the checkpoint
    notes, refusal = ckpt.restore_last(repo, "s4")
    assert refusal and "moved" in refusal
    assert (repo / "seed.txt").read_text() == "user edit\n"          # nothing was touched


def test_no_checkpoint_and_not_a_repo(tmp_path):
    repo = repo_for(tmp_path)
    notes, refusal = ckpt.restore_last(repo, "unknown-session")
    assert notes == [] and refusal and "no checkpoint" in refusal
    assert ckpt.is_repo(repo) and not ckpt.is_repo(tmp_path / "nowhere")