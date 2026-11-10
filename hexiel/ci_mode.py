"""CI runner mode (0.8.7 #41): `hexiel --ci` in a pipeline.

Reads the CI's issue/commit context (GitLab first), runs one headless turn
with read-only tools, and posts the findings back where the team can read
them: a note on the merge request (MR pipelines), a comment on the commit
(commit pipelines), or stdout.

Tokens: a project/repo Personal Access Token (HEXIEL_GITLAB_TOKEN — the same
one /setup-gitlab installs) is preferred. CI_JOB_TOKEN works for the notes
endpoint when the project allows it; failures there print a hint instead of
failing the job — the findings still went to stdout.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

GITLAB_CI = "GITLAB_CI"                        # set inside every GitLab pipeline job


# ---------------------------------------------------------------- context from the environment

def ci_context() -> dict:
    """What to review this run: an MR, a commit, or nothing (plain prompt mode)."""
    if not os.environ.get(GITLAB_CI):
        return {"kind": "none"}
    try:
        api_v4 = os.environ.get("CI_API_V4_URL", "").rstrip("/")
        project_id = os.environ.get("CI_PROJECT_ID", "")
        mr = os.environ.get("CI_MERGE_REQUEST_IID")
        if mr and api_v4 and project_id:
            return {"kind": "mr", "iid": mr, "api_v4": api_v4, "project_id": project_id,
                    "ref": os.environ.get("CI_MERGE_REQUEST_TARGET_BRANCH_NAME", ""),
                    "source_sha": os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA", "")}
        sha = os.environ.get("CI_COMMIT_SHA")
        if sha and api_v4 and project_id:
            return {"kind": "commit", "sha": sha, "api_v4": api_v4, "project_id": project_id,
                    "ref": os.environ.get("CI_COMMIT_REF_NAME", "main")}
    except (ValueError, TypeError):
        pass
    return {"kind": "none"}


def prompt_for(ctx: dict, cwd: str, extra: str) -> str:
    """The headless prompt: the CI context (diff, MR text, commit log) plus what the job asked for."""
    if ctx["kind"] == "mr":
        info = gitlab_mr_info(ctx)
        diff = gitlab_diff(ctx, info.get("diff_base") or ctx.get("source_sha") or "")
        return ("[CI reviewer run] GitLab merge request "
                f"!{ctx['iid']}{(' — ' + info['title']) if info.get('title') else ''}\n"
                f"Branch {info.get('source', '?')} → {ctx.get('ref') or info.get('target', '?')}\n\n"
                f"{(info.get('description') or '')[:2000]}\n\n"
                "Diff (from the merge base):\n"
                f"```\n{diff[:14000]}\n```\n\n"
                "Review it: bugs, regressions, security, naming, missed tests. Nothing changes on disk. "
                "Reply with findings ordered by severity, each with file/line, and say "
                "\"no blocking issues\" when there are none — the reply posts to the MR."
                + (f"\n\nReviewer notes from the pipeline config: {extra}" if extra else ""))
    if ctx["kind"] == "commit":
        info = gitlab_commit_info(ctx)
        diff = gitlab_diff(ctx, info.get("parent") or (f"{ctx['sha']}~1" if ctx.get("sha") else ""))
        sha = ctx.get("sha", "")
        return (f"[CI reviewer run] commit {sha[:12]} — {info.get('title', '')}\n"
                f"branch {ctx.get('ref')}\n\n"
                f"```\n{diff[:14000]}\n```\n\n"
                "Review the change: correctness, security, missed tests, leftovers. Reply ordered "
                "by severity with file/line for each finding."
                + (f"\n\nReviewer notes from the pipeline config: {extra}" if extra else ""))
    return f"[CI run on branch {os.environ.get('CI_COMMIT_REF_NAME', 'main')}]\n{extra}"


# ---------------------------------------------------------------- gitlab reads

def _project_api(ctx: dict) -> str:
    return f"{ctx['api_v4']}/projects/{ctx['project_id']}"


def gitlab_headers() -> dict:
    token = os.environ.get("HEXIEL_GITLAB_TOKEN") or os.environ.get("GITLAB_TOKEN") or ""
    if token:
        return {"PRIVATE-TOKEN": token}
    job = os.environ.get("CI_JOB_TOKEN") or ""
    return {"JOB-TOKEN": job} if job else {}


def gitlab_mr_info(ctx: dict) -> dict:
    import httpx
    try:
        with httpx.Client(timeout=45) as c:
            r = c.get(f"{_project_api(ctx)}/merge_requests/{ctx['iid']}", headers=gitlab_headers())
            if r.status_code == 200:
                d = r.json()
                return {"title": d.get("title", ""), "description": d.get("description", ""),
                        "source": d.get("source_branch", ""), "target": d.get("target_branch", ""),
                        "diff_base": d.get("diff_refs", {}).get("base_sha", "")
                        or ctx.get("source_sha", ""), "web_url": d.get("web_url", "")}
    except (httpx.HTTPError, ValueError):
        pass
    return {}


def gitlab_commit_info(ctx: dict) -> dict:
    import httpx
    try:
        with httpx.Client(timeout=45) as c:
            r = c.get(f"{_project_api(ctx)}/repository/commits/{ctx['sha']}", headers=gitlab_headers())
            if r.status_code == 200:
                d = r.json()
                parents = d.get("parent_ids") or [""]
                return {"sha": ctx["sha"], "title": d.get("title", ""), "parent": parents[0],
                        "web_url": d.get("web_url", "")}
    except (httpx.HTTPError, ValueError):
        pass
    return {}


def gitlab_diff(ctx: dict, base_sha: str) -> str:
    """The diff body from git itself (no API round trip): against the base when known,
    the commit itself otherwise (works for both MR and commit pipelines)."""
    if not base_sha:
        return "(diff unavailable in this pipeline shape)"
    import subprocess
    try:
        proc = subprocess.run(["git", "diff", f"{base_sha}..HEAD"], cwd=os.getcwd(),
                              capture_output=True, text=True, timeout=60)
        if proc.stdout.strip():
            return proc.stdout.strip()
        proc = subprocess.run(["git", "show", ctx.get("sha") or "HEAD"], cwd=os.getcwd(),
                              capture_output=True, text=True, timeout=60)
        return proc.stdout.strip()[:14000]
    except (OSError, subprocess.SubprocessError):
        return "(diff unavailable: git failed)"


# ---------------------------------------------------------------- reporting back

def report(ctx: dict, body: str) -> str:
    """Post `body` to the MR (or note under the commit). Returns where it went — printed
    for the pipeline log. A comment failure only prints; the run itself still worked."""
    import httpx
    where = "stdout only"
    if ctx["kind"] not in ("mr", "commit"):
        return where
    note_ctx = dict(ctx)
    try:
        if ctx["kind"] == "mr":
            url = f"{_project_api(ctx)}/merge_requests/{ctx['iid']}/notes"
        else:
            url = f"{_project_api(ctx)}/repository/commits/{ctx['sha']}/comments"
        if not gitlab_headers():
            return "stdout only (no CI token for a note; set HEXIEL_GITLAB_TOKEN to comment)"
        with httpx.Client(timeout=45) as c:
            r = c.post(url, headers=gitlab_headers(), json={"body": body})
            if r.status_code in (200, 201):
                where = f"posted to {'MR !' + str(ctx['iid']) if ctx['kind'] == 'mr' else 'commit ' + ctx['sha'][:8]}"
            else:
                where = (f"stdout only (the note POST said {r.status_code}: a project token with "
                         f">=reporter may be needed when only CI_JOB_TOKEN is set)")
    except httpx.HTTPError as exc:
        where = f"stdout only (comment posting failed: {type(exc).__name__})"
    return where
