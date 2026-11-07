"""#41 CI runner mode — context collection from GitLab pipeline env, read-only
session construction, prompt assembly, and the comment-back route."""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hexiel import ci_mode


def _env(monkeypatch, **vals):
    for k in ("GITLAB_CI", "CI_API_V4_URL", "CI_PROJECT_ID", "CI_MERGE_REQUEST_IID",
              "CI_MERGE_REQUEST_DIFF_BASE_SHA", "CI_MERGE_REQUEST_TARGET_BRANCH_NAME",
              "CI_COMMIT_SHA", "CI_COMMIT_REF_NAME", "HEXIEL_GITLAB_TOKEN", "GITLAB_TOKEN",
              "CI_JOB_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    for k, v in vals.items():
        monkeypatch.setenv(k, v)


def test_context_none_outside_ci(monkeypatch):
    _env(monkeypatch)
    assert ci_mode.ci_context() == {"kind": "none"}


def test_context_reads_an_mr_pipeline(monkeypatch):
    _env(monkeypatch, GITLAB_CI="true", CI_API_V4_URL="https://gitlab.example/api/v4",
         CI_PROJECT_ID="123", CI_MERGE_REQUEST_IID="42",
         CI_MERGE_REQUEST_DIFF_BASE_SHA="abc123",
         CI_MERGE_REQUEST_TARGET_BRANCH_NAME="main")
    ctx = ci_mode.ci_context()
    assert ctx["kind"] == "mr" and ctx["iid"] == "42" and ctx["source_sha"] == "abc123"


def test_context_reads_a_commit_pipeline(monkeypatch):
    _env(monkeypatch, GITLAB_CI="true", CI_API_V4_URL="https://gitlab.example/api/v4",
         CI_PROJECT_ID="9", CI_COMMIT_SHA="deadbeef", CI_COMMIT_REF_NAME="feature/x")
    ctx = ci_mode.ci_context()
    assert ctx["kind"] == "commit" and ctx["sha"] == "deadbeef" and ctx["ref"] == "feature/x"


def test_mr_prompt_carries_the_review_brief(tmp_path, monkeypatch):
    _env(monkeypatch, GITLAB_CI="true", CI_API_V4_URL="https://gitlab.example/api/v4",
         CI_PROJECT_ID="123", CI_MERGE_REQUEST_IID="42", HEXIEL_GITLAB_TOKEN="glpat-t")
    monkeypatch.setattr("subprocess.run",
                        lambda *a, **k: subprocess.CompletedProcess([], 0, "diff --git a/x b/x", ""))
    ctx = ci_mode.ci_context()
    prompt = ci_mode.prompt_for(ctx, str(tmp_path), "focus on payments")
    assert "merge request !42" in prompt
    assert "review it" in prompt.lower()
    assert "focus on payments" in prompt
    assert "no blocking issues" in prompt


def test_commit_prompt_mentions_the_sha(tmp_path, monkeypatch):
    _env(monkeypatch, GITLAB_CI="true", CI_API_V4_URL="https://gitlab.example/api/v4",
         CI_PROJECT_ID="9", CI_COMMIT_SHA="deadbeef1234", HEXIEL_GITLAB_TOKEN="glpat-t")
    monkeypatch.setattr("subprocess.run",
                        lambda *a, **k: subprocess.CompletedProcess([], 0, "diffbody", ""))
    import httpx

    class FakeResponse:
        status_code = 404
    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **k): return FakeResponse()
    monkeypatch.setattr("httpx.Client", FakeClient)
    prompt = ci_mode.prompt_for(ci_mode.ci_context(), str(tmp_path), "")
    assert "deadbeef" in prompt and "diffbody" in prompt


def test_report_posts_to_the_mr(monkeypatch, tmp_path):
    _env(monkeypatch, GITLAB_CI="true", CI_API_V4_URL="https://gitlab.example/api/v4",
         CI_PROJECT_ID="123", CI_MERGE_REQUEST_IID="42", HEXIEL_GITLAB_TOKEN="glpat-t")
    sent = {}

    class FakeResponse:
        status_code = 201
    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, headers=None, json=None):
            sent["url"], sent["body"], sent["headers"] = url, json["body"], headers
            return FakeResponse()
    monkeypatch.setattr("httpx.Client", FakeClient)
    where = ci_mode.report({"kind": "mr", "iid": "42", "api_v4": "https://gitlab.example/api/v4",
                            "project_id": "123"}, "findings body")
    assert "MR !42" in where and sent["body"] == "findings body"
    assert sent["headers"]["PRIVATE-TOKEN"] == "glpat-t"


def test_report_commit_comments_route(monkeypatch):
    _env(monkeypatch, GITLAB_CI="true", CI_API_V4_URL="https://gitlab.example/api/v4",
         CI_PROJECT_ID="9", CI_COMMIT_SHA="cafe1234", HEXIEL_GITLAB_TOKEN="glpat-t")
    got = {}

    class FakeResponse:
        status_code = 201
    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, headers=None, json=None):
            got["url"] = url
            return FakeResponse()
    monkeypatch.setattr("httpx.Client", FakeClient)
    where = ci_mode.report(ci_mode.ci_context(), "body")
    assert "commit cafe" in where and "/commits/cafe1234/comments" in got["url"]


def test_report_never_crashes_the_pipeline(monkeypatch):
    _env(monkeypatch, GITLAB_CI="true", CI_API_V4_URL="https://gitlab.example/api/v4",
         CI_PROJECT_ID="123", CI_MERGE_REQUEST_IID="42", HEXIEL_GITLAB_TOKEN="glpat-t")

    import httpx
    def boom(*a, **k):
        raise httpx.ConnectError("no dns")
    monkeypatch.setattr("httpx.Client", boom)   # constructing the client raises
    where = ci_mode.report(ci_mode.ci_context(), "body")
    assert where.startswith("stdout only")


def test_job_token_used_when_no_pat(monkeypatch):
    _env(monkeypatch, GITLAB_CI="true", CI_API_V4_URL="https://gitlab.example/api/v4",
         CI_PROJECT_ID="123", CI_MERGE_REQUEST_IID="42", CI_JOB_TOKEN="jobtok")
    got = {}

    class FakeResponse:
        status_code = 201
    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, headers=None, json=None):
            got["headers"] = headers
            return FakeResponse()
    monkeypatch.setattr("httpx.Client", FakeClient)
    assert "MR !42" in ci_mode.report(ci_mode.ci_context(), "b")
    assert got["headers"]["JOB-TOKEN"] == "jobtok"


# ---------------------------------------------------------------- end-to-end shape

def test_ci_end_to_end_is_readonly_headless(tmp_path, monkeypatch):
    """The whole wiring: --ci builds a readonly-only session and one headless prompt."""
    _env(monkeypatch, HEXIEL_GITLAB_TOKEN="")   # no token: stdout-only run
    from hexiel import config as config_mod
    config_mod.CONFIG_FILE.write_text(
        '[settings]\n[models.m]\nprovider = "openai"\nmodel = "m"\nbase_url = ""\n')
    import dataclasses
    from hexiel.main import build_session
    session = build_session(str(tmp_path), "m", readonly_only=True)
    writeish = [t.name for t in session.tools if not t.readonly]
    assert writeish == [], writeish
    readonly = [t.name for t in session.tools]
    assert "diagnostics" in readonly and "task" in readonly


def test_docs_snippet_exists_for_the_job(monkeypatch):
    from hexiel import main as main_mod
    import io, contextlib
    buf = io.StringIO()
    monkeypatch.setattr(sys, "argv", ["hexiel", "--help"])
    try:
        with contextlib.redirect_stdout(buf):
            main_mod.parse_args()
    except SystemExit:
        pass
    assert "--ci" in buf.getvalue() and "CI runner mode" in buf.getvalue()
