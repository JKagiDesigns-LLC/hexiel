"""GitLab collaboration flow: branches, commits, push, and merge requests.

Works for the repo owner (push branch, open MR in place) and for contributors
(fork via API, push to the fork, open an MR from `fork:branch` to upstream —
the classic open-source proposal flow). Auth comes from HEXIEL_GITLAB_TOKEN /
GITLAB_TOKEN env or `gitlab_token` in ~/.config/hexiel/auth.toml; without a
token everything degrades to plain git plus printed manual steps.
"""

import base64
import json
import os
import re
import subprocess
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import httpx


class GitlabError(RuntimeError):
    pass


@dataclass
class GitlabProject:
    host: str                       # gitlab.com or self-hosted base URL
    path: str                       # url-encoded full path (group/sub/repo)
    api_base: str
    token: str = ""
    project: dict = field(default_factory=dict)
    default_branch: str = "main"

    @property
    def project_api(self) -> str:
        return f"{self.api_base}/projects/{self.path}"


def _git(cwd: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=60)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def load_token() -> str:
    for env in ("HEXIEL_GITLAB_TOKEN", "GITLAB_TOKEN"):
        if os.environ.get(env):
            return os.environ[env].strip()
    auth = Path.home() / ".config" / "hexiel" / "auth.toml"
    try:
        import tomllib
        return tomllib.loads(auth.read_text()).get("gitlab", {}).get("token", "").strip()
    except (OSError, KeyError, ValueError):
        return ""


def save_token(token: str, host: str = "gitlab.com") -> Path:
    cfg = Path.home() / ".config" / "hexiel"
    cfg.mkdir(parents=True, exist_ok=True)
    path = cfg / "auth.toml"
    try:
        import tomllib
        existing = tomllib.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        existing = {}
    existing.setdefault("gitlab", {})["token"] = token
    existing["gitlab"]["host"] = host
    text = "# hexiel credentials (0600) — created by /setup-gitlab\n"
    for section, values in existing.items():
        text += f"\n[{section}]\n" + "\n".join(f"{k} = {v!r}" for k, v in values.items()) + "\n"
    path.write_text(text)
    os.chmod(path, 0o600)
    return path


def detect_project(cwd: str, remote: str = "origin") -> GitlabProject | None:
    """Find the gitlab project from a git remote (ssh or https, .com or self-hosted)."""
    origin = _git(cwd, "remote", "get-url", remote)
    if not origin:
        return None
    host, path = _parse_remote(origin)
    if not host or not path:
        return None
    token = load_token()
    api_base = f"https://{host}/api/v4"
    encoded = urllib.parse.quote(path, safe="")
    project = {}
    headers = {"PRIVATE-TOKEN": token} if token else {}
    try:
        with httpx.Client(timeout=20) as client:
            resp = client.get(f"{api_base}/projects/{encoded}", headers=headers)
            project = resp.json() if resp.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        project = {}
    return GitlabProject(host=host, path=encoded, api_base=api_base, token=token,
                         project=project,
                         default_branch=project.get("default_branch", "main") if project else "main")


def _parse_remote(origin: str) -> tuple[str, str]:
    if origin.startswith("ssh://"):
        m = re.match(r"^ssh://git@([^/:]+)(?::\d+)?[:/](.+?)(?:\.git)?/?$", origin)
        if m:
            return m.group(1), m.group(2).rstrip("/")
        return "", ""
    m = re.match(r"^git@([^:/]+):(.+?)(?:\.git)?/?$", origin)
    if m:
        return m.group(1), m.group(2).rstrip("/")
    m = re.match(r"^https?://([^/]+)/(.+?)(?:\.git)?/?$", origin)
    if m:
        return m.group(1), m.group(2).rstrip("/")
    return "", ""


def _api(project: GitlabProject, method: str, url: str, json_body: dict | None = None,
         params: dict | None = None) -> dict:
    if not project.token:
        raise GitlabError(
            "no GitLab token — run /setup-gitlab, or set HEXIEL_GITLAB_TOKEN "
            "(a Personal Access Token with api scope)")
    headers = {"PRIVATE-TOKEN": project.token}
    with httpx.Client(timeout=30) as client:
        resp = client.request(method, url, headers=headers, json=json_body, params=params or {})
    if resp.status_code >= 300:
        try:
            body = resp.json()
        except ValueError:
            body = resp.text[:300]
        raise GitlabError(f"gitlab {method} {resp.status_code}: {body}")
    return resp.json()


# ---------------------------------------------------------------- git ops

def repo_status(cwd: str) -> dict:
    return {
        "branch": _git(cwd, "branch", "--show-current") or "(detached)",
        "dirty_files": _git(cwd, "status", "--porcelain").splitlines(),
        "remote": _git(cwd, "remote", "get-url", "origin"),
    }


def ensure_branch(cwd: str, branch: str) -> str:
    """Create (from origin's default) and switch to branch; returns where it came from."""
    if _branch_exists(cwd, branch):
        _run(cwd, ["git", "checkout", branch])
        return "existing"
    base = _origin_default(cwd)
    _run(cwd, ["git", "checkout", "-b", branch, f"origin/{base}"])
    return f"new from origin/{base}"


def commit_files(cwd: str, message: str, files: list[str] | None = None) -> int:
    """Stage (given files, or everything) and commit; 1 if a commit was made."""
    if files:
        _run(cwd, ["git", "add", "--", *files])
    else:
        _run(cwd, ["git", "add", "-A"])
    if _git(cwd, "diff", "--cached", "--name-only"):
        _run(cwd, ["git", "commit", "-m", message])
        return 1
    return 0


def push(cwd: str, branch: str, remote: str = "origin") -> str:
    return _run(cwd, ["git", "push", "-u", remote, branch])


def _branch_exists(cwd: str, branch: str) -> bool:
    return bool(_git(cwd, "branch", "--list", branch))


def _origin_default(cwd: str) -> str:
    return _git(cwd, "rev-parse", "--abbrev-ref", "origin/HEAD").removeprefix("origin/") or "main"


def _run(cwd: str, args: list[str]) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise GitlabError(f"git {' '.join(args[:3])} failed: {(proc.stderr or proc.stdout)[:400]}")
    return (proc.stdout or proc.stderr).strip()


# ---------------------------------------------------------------- MR flow

def propose_change(cwd: str, title: str, body: str, *, branch: str | None = None,
                   files: list[str] | None = None, on_step=None,
                   project: GitlabProject | None = None) -> dict:
    """Full propose flow for the repo owner: branch -> commit -> push -> MR.
    Each step passes through on_step(kind, description) -> bool so callers can
    gate approvals. Returns {branch, mr_iid, mr_url, ...}."""
    project = project or detect_project(cwd)
    if project is None:
        raise GitlabError("no gitlab remote detected (add one: git remote add origin <url>)")
    steps: list[tuple[str, str, bool]] = []

    def gate(kind: str, description: str) -> bool:
        ok = on_step(kind, description) if on_step else True
        steps.append((kind, description, ok))
        return ok

    if project.project and _git(cwd, "remote", "get-url", "origin"):
        try:
            subprocess.run(["git", "-C", cwd, "fetch", "--quiet", "origin"],
                           capture_output=True, timeout=120)
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            pass
    branch = branch or f"hexiel/{_slug(title)}"
    default = project.default_branch

    if not gate("branch", f"create/switch branch {branch} from origin/{default}"):
        return {"branch": branch, "pushed": False, "steps": steps}
    ensure_branch(cwd, branch)

    dirty = [ln[3:].strip() for ln in _git(cwd, "status", "--porcelain").splitlines()]
    if dirty:
        names = files or dirty
        if gate("commit", f"commit {len(names)} changed file(s): "
                          + ", ".join(names[:6]) + ("…" if len(names) > 6 else "")):
            commit_files(cwd, f"{title}\n\n{body}\n\nCo-Authored-By: hexiel agent",
                         files=names)
    if not gate("push", f"push {branch} to origin"):
        return {"branch": branch, "pushed": False, "steps": steps}
    push(cwd, branch)

    mr = open_mr(project, branch, title, body)
    return {"branch": branch, "mr_iid": mr.get("iid"),
            "mr_url": mr.get("web_url", ""), "pushed": True, "steps": steps}


def open_mr(project: "GitlabProject", branch: str, title: str, body: str) -> dict:
    """Open an MR in the detected project (owner pushing a branch to origin).
    Contributors fork first (below) and open the MR from their fork."""
    mr = {
        "source_branch": branch,
        "target_branch": project.default_branch,
        "title": title,
        "description": body,
        "remove_source_branch": True,
    }
    return _api(project, "POST", f"{project.project_api}/merge_requests", mr)


def propose_from_fork(cwd: str, upstream: "GitlabProject", title: str, body: str,
                      on_step=None) -> dict:
    """Contributor flow: fork via API, push the fork, open MR from fork branch.
    Each step gated by on_step(kind, description) -> bool like propose_change."""
    steps: list[tuple[str, str, bool]] = []

    def gate(kind: str, description: str) -> bool:
        ok = on_step(kind, description) if on_step else True
        steps.append((kind, description, ok))
        return ok

    branch = f"hexiel/{_slug(title)}"
    if not gate("fork", f"fork {upstream.path} under your account"):
        raise GitlabError("fork step declined")
    forked = _api(upstream, "POST", f"{upstream.api_base}/projects/{upstream.path}/fork")
    remote_url = _https_remote(upstream.host, forked["path_with_namespace"], upstream.token)
    if not gate("push", f"push branch {branch} to your fork ({forked['path_with_namespace']})"):
        raise GitlabError("push step declined")
    try:
        _run(cwd, ["git", "remote", "remove", "hexiel-fork"])
    except GitlabError:
        pass
    _run(cwd, ["git", "remote", "add", "hexiel-fork", remote_url])
    current = _git(cwd, "branch", "--show-current")
    if current != branch:
        ensure_branch(cwd, branch)
    commit_files(cwd, f"{title}\n\n{body}\n\nCo-Authored-By: hexiel agent")
    push(cwd, branch, remote="hexiel-fork")
    if not gate("mr", f"open merge request from fork:{branch} into upstream"):
        raise GitlabError("mr step declined")
    mr = {
        "source_branch": branch,
        "target_branch": _origin_default(cwd),
        "title": title,
        "description": body,
        "source_project_id": forked["id"],
        "target_project_id": upstream.project.get("id"),
        "remove_source_branch": False,
    }
    created = _api(upstream, "POST",
                   f"{upstream.api_base}/projects/{upstream.path}/merge_requests", mr)
    result = {"branch": branch, "fork": forked["path_with_namespace"],
              "mr_iid": created.get("iid"), "mr_url": created.get("web_url", ""),
              "steps": steps}
    if current and current != branch:
        try:
            _run(cwd, ["git", "checkout", current])
        except GitlabError:
            pass
    return result


def _https_remote(host: str, path: str, token: str) -> str:
    encoded = f"hexiel:{token}" if token else "hexiel"
    return f"https://{encoded}@{host}/{path}.git"


def mr_status(cwd: str, mr_iid: int, project: GitlabProject | None = None) -> dict:
    project = project or detect_project(cwd)
    detail = _api(project, "GET", f"{project.project_api}/merge_requests/{mr_iid}")
    return {
        "state": detail.get("state"),
        "approved": detail.get("approved_by", []),
        "pipeline": detail.get("head_pipeline", {}).get("status") if detail.get("head_pipeline") else None,
        "mergeable": detail.get("merge_status"),
        "url": detail.get("web_url", ""),
        "title": detail.get("title", ""),
    }


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
    return f"{slug}-{base64.b32encode(os.urandom(2)).decode().lower()}"