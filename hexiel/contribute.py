"""Self-heal workspaces and sharing fixes back to hexiel.

/heal never edits the user's project or their install directly. It works in an
isolated git worktree of hexiel's *own* source, at the exact version that is
running:

  * git checkout install  -> worktree of that checkout's HEAD
  * pip install           -> a cached clone of the official repo at tag vX.Y.Z

After the agent fixes and tests the bug there, the user reviews the diff and
picks which files to keep. Then:

  * keep   -> copy the chosen files into the running install (fix applies now)
  * send   -> commit only the chosen files and open a merge request to the
              official hexiel repo (direct branch for maintainers, fork for
              everyone else) — so every hexiel user gets the fix
"""

import datetime
import shutil
import subprocess
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import __version__
from .config import home_data_dir
from .gitlab import GitlabError, GitlabProject, _api, _https_remote, load_token

UPSTREAM_HOST = "gitlab.com"
UPSTREAM_PATH = "jkagidesigns1/public/applications/hexiel"
UPSTREAM_GIT = f"https://{UPSTREAM_HOST}/{UPSTREAM_PATH}.git"
PACKAGE_DIR = Path(__file__).resolve().parent          # .../hexiel (the package)
INSTALL_ROOT = PACKAGE_DIR.parent                       # repo root for a git checkout


def heal_root() -> Path:
    return home_data_dir() / "heal"


def _git(cwd: Path | str, *args: str, check: bool = True, timeout: int = 180) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          timeout=timeout)
    if check and proc.returncode != 0:
        raise GitlabError(f"git {' '.join(args[:3])} failed: {(proc.stderr or proc.stdout).strip()[:400]}")
    return proc.stdout


def is_git_install() -> bool:
    return (INSTALL_ROOT / ".git").exists()


@dataclass
class Workspace:
    path: Path
    source: Path          # the repo the worktree belongs to
    branch: str
    base: str             # commit the fix is based on
    slug: str


def prepare(slug: str | None = None) -> Workspace:
    """Create a clean worktree of the running hexiel's source to heal in."""
    slug = slug or datetime.datetime.now().strftime("fix-%Y%m%d-%H%M%S")
    if is_git_install():
        source, base = INSTALL_ROOT, _git(INSTALL_ROOT, "rev-parse", "HEAD").strip()
    else:
        source = heal_root() / "src"
        if not (source / ".git").exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            _git(source.parent, "clone", "--quiet", "--no-checkout", UPSTREAM_GIT, str(source), timeout=600)
        _git(source, "fetch", "--quiet", "--tags", "origin", timeout=600)
        tag = f"v{__version__}"
        has_tag = _git(source, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}", check=False).strip()
        base = _git(source, "rev-parse", tag if has_tag else "origin/main").strip()
    path = heal_root() / "work" / slug
    path.parent.mkdir(parents=True, exist_ok=True)
    branch = f"hexiel-fix/{slug}"
    _git(source, "worktree", "add", "--quiet", "-b", branch, str(path), base)
    return Workspace(path=path, source=source, branch=branch, base=base, slug=slug)


def discard(ws: Workspace) -> None:
    _git(ws.source, "worktree", "remove", "--force", str(ws.path), check=False)
    _git(ws.source, "branch", "-D", ws.branch, check=False)
    shutil.rmtree(ws.path, ignore_errors=True)


# ---------------------------------------------------------------- reviewing

@dataclass
class Change:
    path: str
    status: str          # "modified" | "added" | "deleted"
    added: int = 0
    removed: int = 0
    selected: bool = True
    diff: str = field(default="", repr=False)


def changes(ws: Workspace) -> list[Change]:
    """Every changed file in the workspace (tracked edits, new files, deletions)."""
    out: list[Change] = []
    _git(ws.path, "add", "--intent-to-add", "--all")        # make new files diffable
    kinds = {"A": "added", "D": "deleted"}
    status_of = {}
    for line in _git(ws.path, "diff", "--name-status", ws.base).splitlines():
        code, *paths = line.split("\t")
        status_of[paths[-1]] = kinds.get(code[:1], "modified")
    for line in _git(ws.path, "diff", "--numstat", ws.base).splitlines():
        added, removed, path = line.split("\t", 2)
        status = status_of.get(path, "modified")
        out.append(Change(path=path, status=status,
                          added=int(added) if added.isdigit() else 0,
                          removed=int(removed) if removed.isdigit() else 0,
                          diff=_git(ws.path, "diff", ws.base, "--", path)))
    return out


# ---------------------------------------------------------------- keeping

def keep(ws: Workspace, selected: list[Change]) -> list[str]:
    """Apply the chosen files to the running install. Returns what was applied."""
    applied = []
    for ch in selected:
        if is_git_install():
            target = INSTALL_ROOT / ch.path
        elif ch.path.startswith("hexiel/"):
            target = PACKAGE_DIR / ch.path[len("hexiel/"):]
        else:
            continue            # tests/docs aren't part of a pip install
        if ch.status == "deleted":
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ws.path / ch.path, target)
        applied.append(ch.path)
    return applied


# ---------------------------------------------------------------- sending

def _upstream(token: str) -> GitlabProject:
    encoded = urllib.parse.quote(UPSTREAM_PATH, safe="")
    api_base = f"https://{UPSTREAM_HOST}/api/v4"
    with httpx.Client(timeout=20) as client:
        resp = client.get(f"{api_base}/projects/{encoded}", headers={"PRIVATE-TOKEN": token})
    project = resp.json() if resp.status_code == 200 else {}
    return GitlabProject(host=UPSTREAM_HOST, path=encoded, api_base=api_base, token=token,
                         project=project, default_branch=project.get("default_branch", "main"))


def can_push(project: GitlabProject) -> bool:
    perms = project.project.get("permissions") or {}
    levels = [(perms.get(k) or {}).get("access_level", 0) for k in ("project_access", "group_access")]
    return max(levels) >= 30          # Developer or higher may push branches


def send(ws: Workspace, selected: list[Change], title: str, body: str) -> dict:
    """Commit only the selected files and open a merge request to official hexiel."""
    token = load_token()
    if not token:
        raise GitlabError("sharing needs a free GitLab account: create a Personal Access Token "
                          "with `api` scope at https://gitlab.com/-/user_settings/personal_access_tokens "
                          "and run /setup-gitlab <token> — then choose Send again")
    if not selected:
        raise GitlabError("no files selected")
    _git(ws.path, "reset", "--quiet")                               # unstage everything …
    for ch in selected:                                             # … stage only the chosen files
        _git(ws.path, "add", "--all", "--", ch.path)
    _git(ws.path, "-c", "user.name=hexiel self-heal", "-c", "user.email=self-heal@hexiel.tech",
         "commit", "--quiet", "-m", f"{title}\n\n{body}\n\nProposed by hexiel {__version__} self-heal")

    upstream = _upstream(token)
    if not upstream.project:
        raise GitlabError("could not reach the official hexiel project on GitLab")
    if can_push(upstream):
        remote = _https_remote(UPSTREAM_HOST, UPSTREAM_PATH, token)
        source_project_id, where = upstream.project["id"], "hexiel"
    else:
        try:
            fork = _api(upstream, "POST", f"{upstream.api_base}/projects/{upstream.path}/fork")
        except GitlabError as exc:
            if "already been taken" not in str(exc) and "409" not in str(exc):
                raise
            me = _api(upstream, "GET", f"{upstream.api_base}/user")
            fork = _api(upstream, "GET", f"{upstream.api_base}/projects/"
                        + urllib.parse.quote(f"{me['username']}/hexiel", safe=""))
        remote = _https_remote(UPSTREAM_HOST, fork["path_with_namespace"], token)
        source_project_id, where = fork["id"], fork["path_with_namespace"]
    _git(ws.path, "push", "--quiet", remote, f"HEAD:refs/heads/{ws.branch}", timeout=300)
    mr = _api(upstream, "POST", f"{upstream.api_base}/projects/{upstream.path}/merge_requests", {
        "source_branch": ws.branch, "target_branch": upstream.default_branch,
        "title": title, "description": body,
        "source_project_id": source_project_id, "target_project_id": upstream.project["id"],
        "remove_source_branch": True,
    })
    return {"mr_url": mr.get("web_url", ""), "mr_iid": mr.get("iid"), "pushed_to": where,
            "branch": ws.branch, "files": [c.path for c in selected]}


def mr_body(description: str, incidents: list[dict], selected: list[Change]) -> str:
    lines = ["## Self-heal fix", "", description or "Fix for recorded hexiel incidents.", ""]
    if incidents:
        lines += ["## Incidents", ""]
        for inc in incidents[:5]:
            lines.append(f"- **{inc.get('kind')}** (hexiel {inc.get('hexiel_version')}): "
                         f"{str(inc.get('detail', ''))[:300]}")
        lines.append("")
    lines += ["## Files", ""] + [f"- `{c.path}` ({c.status}, +{c.added} −{c.removed})" for c in selected]
    lines += ["", f"_Reviewed and sent by the user from hexiel {__version__}'s self-heal._"]
    return "\n".join(lines)
