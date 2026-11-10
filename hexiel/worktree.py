"""Git worktree isolation (0.8.7): parallel agent runs never collide on one checkout.

/wt lists, creates, opens and removes worktrees; the `task` tool can give a
subagent its own worktree; headless runs accept --worktree. Worktrees live in a
sibling folder of the repo (same filesystem, so builds stay fast) named
`.hexiel-wt/<name>`, outside the repo so nothing accidental gets committed.

Untracked config files that builds need (top-level `.env*`) are copied into a
new worktree (Devin Local style), and a `WorktreePostSetup` hook can run your
own post-create commands (npm install, .venv setup, ...) with
`worktree: <path>` in its payload.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

BRANCH_PREFIX = "hexiel/"          # branches this module creates for worktrees
MANAGER_DIR = ".hexiel-wt"         # sibling of the repo: same filesystem, never committed


def git(cwd: str, *args: str, check: bool = True) -> str:
    """One git command in `cwd`; stdout text or a subprocess.CalledProcessError with .stderr."""
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 and check:
        raise subprocess.CalledProcessError(proc.returncode, "git", output=proc.stdout, stderr=proc.stderr)
    return proc.stdout


def is_repo(cwd: str) -> bool:
    return git(cwd, "rev-parse", "--is-inside-work-tree", check=False).strip() == "true"


def worktrees_root(cwd: str) -> Path:
    """Where this repo's isolated worktrees live: `<repo>/../.hexiel-wt` when the parent
    is writable, else the data dir (same layout on a different filesystem)."""
    repo = Path(cwd)
    sibling = repo.parent / MANAGER_DIR / repo.name
    try:
        sibling.mkdir(parents=True, exist_ok=True)
        probe = sibling / ".w"
        probe.write_text("x")
        probe.unlink()
        return sibling
    except OSError:                                    # read-only parent (e.g. npm-global style trees)
        from .config import home_data_dir
        return home_data_dir() / "worktrees" / repo.name


def slug(name: str) -> str:
    """A filesystem-safe worktree name."""
    safe = "".join(c if c.isalnum() or c in ("-", "_", ".") else "-" for c in name.strip()).strip("-.")
    return safe[:48] or "wt"


@dataclass
class Worktree:
    path: Path
    head: str
    branch: str            # "" = detached HEAD
    bare: bool = False
    is_main: bool = False


def list_worktrees(cwd: str) -> list[Worktree]:
    """`git worktree list --porcelain`, parsed; main worktree first."""
    out: list[Worktree] = []
    path = head = branch = ""
    detached = bare = False

    def flush() -> None:
        nonlocal path, head, branch, detached, bare
        if path:
            out.append(Worktree(path=Path(path), head=head, branch="" if detached else branch,
                                bare=bare, is_main=len(out) == 0))
        path = head = branch = ""
        detached = bare = False

    for line in git(cwd, "worktree", "list", "--porcelain").splitlines():
        if not line.strip():
            flush()
            continue
        if line.startswith("worktree "):
            path = line[9:]
        elif line.startswith("HEAD "):
            head = line[5:]
        elif line.startswith("branch "):
            branch = line[len("branch "):].removeprefix("refs/heads/")
        elif line == "detached":
            detached = True
        elif line == "bare":
            bare = True
    flush()
    return [w for w in out if not w.bare]


def find(cwd: str, name: str, exact: bool = False) -> Worktree | None:
    """A worktree by exact name/path, or (unless `exact`) by a prefix that picks ONE worktree: an ambiguous
    prefix finds nothing rather than whichever came first."""
    want = slug(name)
    worktrees = list_worktrees(cwd)
    for w in worktrees[1:]:                            # never target the main tree
        if w.path.name == want or w.path == Path(name):
            return w
    if exact:
        return None
    hits = [w for w in worktrees[1:]
            if w.path.name.startswith(want) or w.branch == name.removeprefix(BRANCH_PREFIX)
            or w.branch.removeprefix(BRANCH_PREFIX) == name]
    return hits[0] if len(hits) == 1 else None


def unused_number(root: Path, base: str) -> int:
    """First unused `<base>-N` inside `root` (names for ephemeral worktrees)."""
    n = 1
    while (root / f"{base}-{n}").exists() and n < 500:
        n += 1
    return n


def create(cwd: str, name: str, branch: str = "", base: str = "", hooks: list = (),
           session_id: str = "", on_note=None) -> Worktree:
    """A worktree with your own name (or an unused `wt-N` when name is empty), on a fresh
    branch `hexiel/<name>` unless a branch is given. Untracked .env-style files are copied in
    (Devin Local style), then WorktreePostSetup hooks run in it. Returns the new Worktree."""
    repo = Path(cwd)
    root = worktrees_root(cwd)
    wt_name = slug(name) if name else f"wt-{unused_number(root, 'wt')}"
    path = root / wt_name
    if path.exists():
        raise ValueError(f"worktree path already exists: {path}")
    branch = branch or f"{BRANCH_PREFIX}{wt_name}"
    add = ["worktree", "add", "--quiet", "-b", branch, str(path)]
    if base:
        add.append(base)
    try:
        git(cwd, *add)
    except subprocess.CalledProcessError:              # a branch with that name exists: reuse it
        git(cwd, "worktree", "add", "--quiet", str(path), branch)
    copied = copy_env_files(repo, path)
    from . import hooks as hooks_mod
    for out in hooks_mod.run(hooks, "WorktreePostSetup", str(path), session_id, worktree=str(path)):
        if on_note:
            for msg in (out.stdout, out.stderr):
                if msg and msg.strip():
                    on_note(msg.strip())
    if on_note and copied:
        on_note(f"copied untracked config files into the worktree: {', '.join(copied)}")
    return next(w for w in list_worktrees(cwd) if w.path == path)


def copy_env_files(repo: Path, target: Path) -> list[str]:
    """Top-level untracked .env-style files a build/test run needs (config, not code)."""
    copied = []
    try:
        tracked = set(git(str(repo), "ls-files").splitlines())
    except subprocess.CalledProcessError:
        tracked = set()
    try:
        for entry in sorted(repo.glob(".env*")):
            if entry.name.endswith(".example") or entry.is_file() is False:
                continue
            if entry.name in tracked:
                continue                                # tracked env files come with the checkout
            dest = target / entry.name
            if not dest.exists():
                shutil.copy2(entry, dest)
                copied.append(entry.name)
    except OSError:
        pass
    return copied


def open_session_cwd_for(cwd: str, name: str) -> str:
    """Resolve a /wt open target: the worktree path, or a ValueError your UI shows."""
    w = find(cwd, name)
    if w is None:
        raise ValueError(f"no worktree matches {name!r} — /wt lists them")
    return str(w.path)


def remove(cwd: str, name: str, force: bool = True) -> str:
    """Remove a worktree (by name/path) and, when this module made its branch, the branch too.
    Returns a human line."""
    w = find(cwd, name, exact=True)         # removal discards work: never guess from a prefix
    if w is None:
        raise ValueError(f"no worktree is named exactly {name!r} — /wt lists them (removal needs the full name)")
    path, branch = w.path, w.branch
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(path))
    try:
        git(cwd, *args)
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"could not remove {path}: {exc.stderr.strip()[:160]}") from exc
    note = f"removed worktree {path.name}"
    if branch.startswith(BRANCH_PREFIX):               # only ever ours to clean up
        git(cwd, "branch", "-D", branch, check=False)
        note += f" and its branch {branch}"
    return note


def prune(cwd: str) -> str:
    git(cwd, "worktree", "prune", "--verbose", check=False)
    return "pruned stale worktree records"


def repo_slug_for(cwd: str) -> str:
    """A stable short id for a repo path (used to disambiguate two same-named repos)."""
    return hashlib.sha256(str(Path(cwd).resolve()).encode()).hexdigest()[:8]
