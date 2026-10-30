"""Durable turn checkpoints: the workspace as each hexiel turn begins, pinned on PRIVATE git refs,
so /undo still works after the process left. Shape (Cline's, minus the shadow repo): one commit per
checkpoint with three parents — HEAD, the tracked working tree (`git stash create`), and the
untracked non-ignored files — pinned under refs/hexiel/checkpoints/<session>/<ms>. Nothing lands on
the user's branch, stash list, or index; files only ever differ in `git ls-files` space."""

import os
import subprocess
import time

REF_BASE = "refs/hexiel/checkpoints"


def _git(repo, *args, env=None, stdin=None):
    full = dict(os.environ)
    if env:
        full.update({k: str(v) for k, v in env.items()})
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                              env=full, input=stdin, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return 1, "", "git unavailable"
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def is_repo(repo) -> bool:
    return _git(repo, "rev-parse", "--git-dir")[0] == 0


def _tree_of(repo, commit: str) -> str | None:
    code, out, _ = _git(repo, "show", "-s", "--format=%T", commit)
    return out if code == 0 and len(out) == 40 else None


def snapshot(repo, session_id: str, when: float | None = None) -> str | None:
    """Pin the workspace now: returns the ref name, or None when this isn't a usable repo."""
    code, head, _err = _git(repo, "rev-parse", "HEAD")
    if code != 0 or len(head) != 40:
        return None                               # no commits yet: a restore would have nothing to mean
    _, gitdir, _ = _git(repo, "rev-parse", "--git-dir")
    gitdir = os.path.abspath(gitdir)
    _, stash, _ = _git(repo, "stash", "create")
    tracked_tree = _tree_of(repo, stash) if stash else _tree_of(repo, head)
    if tracked_tree is None:
        return None
    parents = [head]
    _, others, _ = _git(repo, "ls-files", "--others", "--exclude-standard")
    if others:
        env = {"GIT_INDEX_FILE": os.path.join(gitdir, "hexiel-checkpoint-index")}
        _git(repo, "read-tree", "--empty", env=env)
        if _git(repo, "update-index", "--add", "--stdin", env=env, stdin=others)[0] == 0:
            code, tree, _ = _git(repo, "write-tree", env=env)
            if code == 0:
                code, commit, _ = _git(repo, "commit-tree", tree, "-m", "untracked files at the checkpoint")
                if code == 0:
                    parents.append(commit)
        try:                                      # a left-over scratch index must never confuse the user's git
            os.unlink(os.path.join(gitdir, "hexiel-checkpoint-index"))
        except OSError:
            pass
    args = ["commit-tree", tracked_tree]
    for p in parents:
        args += ["-p", p]
    code, commit, _ = _git(repo, *args, "-m", "hexiel turn checkpoint")
    if code != 0:
        return None
    ref = f"{REF_BASE}/{session_id}/{int((when or time.time()) * 1000)}"
    code, _, _ = _git(repo, "update-ref", ref, commit)
    return ref if code == 0 else None


def latest(repo, session_id: str) -> str | None:
    code, out, _ = _git(repo, "for-each-ref", f"{REF_BASE}/{session_id}/",
                        "--sort=-refname", "--format=%(refname)")
    if code != 0:
        return None
    lines = out.splitlines()
    return lines[0] if lines else None


def restore_last(repo, session_id: str) -> tuple[list[str], str]:
    """(notes, refusal): bring the working tree back to the most recent checkpoint. Refused (with a
    reason, nothing touched) when a commit was made since — the user's history must never be dropped."""
    ref = latest(repo, session_id)
    if not ref:
        return [], "no checkpoint for this session"
    code, commit, _ = _git(repo, "rev-parse", ref)
    if code != 0:
        return [], "no checkpoint for this session"
    code, base_at_snapshot, _ = _git(repo, "rev-parse", f"{commit}^1")
    code, head_now, _ = _git(repo, "rev-parse", "HEAD")
    if code != 0 or base_at_snapshot != head_now:
        return [], ("HEAD moved since the checkpoint (a commit was made) — refuse; "
                    "use git yourself to keep that commit's work")
    changed, created = set(), set()
    code, files, _ = _git(repo, "diff", "--name-only", ref)            # tracked: ref vs the working tree
    if code == 0 and files:
        changed.update(f for f in files.splitlines() if f)
    _, others, _ = _git(repo, "ls-files", "--others", "--exclude-standard")   # untracked: created or modified since
    if others:
        changed.update(f for f in others.splitlines() if f in _untracked_at(repo, commit))
        created.update(f for f in others.splitlines()
                       if f not in _untracked_at(repo, commit) and os.path.exists(os.path.join(repo, f)))
    notes = []
    for name in sorted(changed):
        source = ref
        if _git(repo, "cat-file", "-e", f"{commit}:{name}")[0] != 0:
            source = f"{commit}^3"                                    # it lived with the untracked parent
        ok, _, _ = _git(repo, "checkout", source, "--", name) if source == ref else (1, "", "")
        if ok != 0:
            ok, _, _ = _git(repo, "checkout", f"{commit}^3", "--", name)
        if ok == 0:
            notes.append(f"restored {name}")
    for name in sorted(created):
        try:
            os.unlink(os.path.join(repo, name))
        except OSError as exc:
            notes.append(f"could not remove {name}: {exc}")
            continue
        notes.append(f"removed {name} (created after the checkpoint)")
    return notes, "" if notes else "nothing changed since the checkpoint"


def _untracked_at(repo, commit: str) -> set[str]:
    """The untracked-file paths a checkpoint recorded (parent 3's tree), as a set."""
    code, out, _ = _git(repo, "ls-tree", "-r", "--name-only", f"{commit}^3")
    return set(out.splitlines()) if code == 0 else set()