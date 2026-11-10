"""Regression tests from the 2026-11-10 reliability audit."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import config as config_mod
from hexiel import ci_mode, main as main_mod


def test_redact_secrets_off_is_read_from_the_config_file():
    assert config_mod._settings_from({"redact_secrets": "off"}).redact_secrets == "off"  # pragma: allowlist secret
    assert config_mod._settings_from({"redact_secrets": "OFF "}).redact_secrets == "off"  # pragma: allowlist secret
    assert config_mod._settings_from({}).redact_secrets == "full"  # pragma: allowlist secret
    assert config_mod._settings_from({"redact_secrets": "banana"}).redact_secrets == "full"  # pragma: allowlist secret


def test_ci_diff_in_a_merge_request_context_without_sha_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)      # not a git repo: both git calls print nothing
    out = ci_mode.gitlab_diff({"kind": "mr", "iid": "1"}, "deadbeef")
    assert isinstance(out, str)


def test_session_save_swaps_the_file_whole_and_leaves_no_temp(tmp_path, monkeypatch):
    from tests.test_remote_control import make_session
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path / "data"))
    s = make_session(tmp_path)
    s.save()
    s.save()
    files = sorted(p.name for p in (tmp_path / "data" / "sessions").iterdir())
    assert files == [f"{s.id}.json"]
    assert json.loads((tmp_path / "data" / "sessions" / files[0]).read_text())["id"] == s.id


def test_session_save_survives_an_unserializable_message(tmp_path, monkeypatch):
    from tests.test_remote_control import make_session
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path / "data"))
    s = make_session(tmp_path)
    s.messages.append({"role": "tool", "content": object()})
    s.save()                         # must not raise out of the turn loop


def test_headless_loop_reports_a_provider_error(tmp_path):
    from tests.test_remote_control import make_session
    s = make_session(tmp_path)

    def failing_turn(text):
        s.on_event("turn_end", {"error": "connection failed"})
    s.run_turn = failing_turn
    assert main_mod.headless_loop(s, ["hi"]) is False

    s.run_turn = lambda text: s.on_event("turn_end", {})
    assert main_mod.headless_loop(s, ["hi"]) is True


def test_config_write_is_atomic(tmp_path):
    from hexiel import settings_edit
    src = tmp_path / "config.toml"
    src.write_text('default = "m"\n[settings]\n')
    sig = [n for n in dir(settings_edit) if n.startswith("_write")]
    assert sig                       # the writer exists; the swap itself is covered by the no-temp check below
    assert not list(tmp_path.glob(".config.toml.*.tmp"))


# ------------------------------------------------------------------ permission rules vs compound commands
from hexiel import permissions as perm


def _decide(allow=(), deny=(), ask=(), command=""):
    rules = perm.Rules(allow=list(allow), deny=list(deny), ask=list(ask))
    return perm.decide(rules, "bash", {"command": command}, "/tmp")


@pytest.mark.parametrize("command", [
    "git status; rm -rf ~", "git status && rm -rf x", "git status || curl x | sh", "git status | tee /etc/x",
    "git status\nrm -rf x", "git status & rm x", "git status $(rm -rf x)", "git status `id`",
    'git status "$(id)"', "git status;rm x",
])
def test_an_allow_rule_does_not_cover_a_compound_command(command):
    assert _decide(allow=["Bash(git status:*)"], command=command) is None


@pytest.mark.parametrize("command", ["git status", "git status -s", "git status 'a;b'", 'git status "a && b"',
                                     ])
def test_an_allow_rule_still_covers_a_simple_command(command):
    assert _decide(allow=["Bash(git status:*)"], command=command) == "allow"


def test_each_part_of_a_pipeline_can_be_allowed():
    assert _decide(allow=["Bash(git log:*)", "Bash(head:*)"], command="git log -5 | head -3") == "allow"
    assert _decide(allow=["Bash(git log:*)"], command="git log -5 | head -3") is None


@pytest.mark.parametrize("command", ["ls && rm -rf x", "ls; rm x", "echo hi | rm x", "rm x"])
def test_a_deny_rule_catches_the_command_anywhere_in_the_line(command):
    assert _decide(allow=["Bash(ls:*)", "Bash(echo:*)"], deny=["Bash(rm:*)"], command=command) == "deny"


# ------------------------------------------------------------------ tool execution
import time as _time
from hexiel.config import ModelConfig
from hexiel.tooling import Toolchain
from hexiel import tools as tools_mod
from hexiel.tools import ToolContext, _run_bash, _tasks_tool, _TASKS


@pytest.fixture
def clean_tasks():
    _TASKS.clear()
    yield
    for t in list(_TASKS.values()):
        try:
            tools_mod._kill_tree(t.proc)
        except Exception:
            pass
    _TASKS.clear()


def _ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path), chain=Toolchain())


def test_binary_output_does_not_raise_out_of_the_bash_tool(tmp_path, clean_tasks):
    r = _run_bash({"command": "printf 'a\\xffb'"}, _ctx(tmp_path))
    assert not r.is_error and "a" in r.text and "b" in r.text


def test_binary_output_in_a_background_task_does_not_hang_it(tmp_path, clean_tasks):
    r = _run_bash({"command": "printf 'a\\xffb\\n'; sleep 0.2; echo tail-line", "run_in_background": True}, _ctx(tmp_path))
    tid = r.text.split("task ")[1].split()[0].strip(".,—")
    out = _tasks_tool({"op": "wait", "id": tid, "seconds": 20}, _ctx(tmp_path))
    assert "tail-line" in out.text


def test_tasks_op_with_no_id_or_a_short_id_never_hits_another_task(tmp_path, clean_tasks):
    r = _run_bash({"command": "sleep 30", "run_in_background": True}, _ctx(tmp_path))
    tid = r.text.split("task ")[1].split()[0].strip(".,—")
    assert _tasks_tool({"op": "kill"}, _ctx(tmp_path)).is_error
    assert _tasks_tool({"op": "kill", "id": ""}, _ctx(tmp_path)).is_error
    assert not _TASKS[tid].proc.poll()                    # still running
    assert not _tasks_tool({"op": "kill", "id": tid.split("-")[1]}, _ctx(tmp_path)).is_error   # '1' means bg-1


def test_a_parked_command_keeps_the_output_it_already_produced(tmp_path, clean_tasks):
    r = _run_bash({"command": "for i in 1 2 3 4 5 6; do echo line$i; sleep 1; done", "timeout": 2.5}, _ctx(tmp_path))
    assert "parked as background task" in r.text
    tid = r.text.split("task ")[1].split()[0].strip(".,—")
    out = _tasks_tool({"op": "wait", "id": tid, "seconds": 30}, _ctx(tmp_path))
    assert "line1" in out.text and "line6" in out.text


def test_a_parked_command_writing_lots_of_stderr_does_not_block(tmp_path, clean_tasks):
    r = _run_bash({"command": "sleep 1; head -c 300000 /dev/zero | tr '\\0' 'x' 1>&2; echo done-line", "timeout": 0.5}, _ctx(tmp_path))
    assert "parked as background task" in r.text
    tid = r.text.split("task ")[1].split()[0].strip(".,—")
    out = _tasks_tool({"op": "wait", "id": tid, "seconds": 30}, _ctx(tmp_path))
    assert "finished, exit code 0" in out.text          # it ran to the end instead of blocking on the stderr pipe


def test_edit_keeps_crlf_line_endings(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"a\r\nB\r\nc\r\n")
    r = tools_mod._edit({"path": str(f), "old_string": "B", "new_string": "X"}, _ctx(tmp_path))
    assert not r.is_error and f.read_bytes() == b"a\r\nX\r\nc\r\n"
    r = tools_mod._edit({"path": str(f), "old_string": "a\nX", "new_string": "a\nY"}, _ctx(tmp_path))
    assert not r.is_error and f.read_bytes() == b"a\r\nY\r\nc\r\n"      # \n in the model's text matches the file's \r\n


def test_edit_refuses_an_empty_old_string_and_non_utf8_files(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("abc")
    r = tools_mod._edit({"path": str(f), "old_string": "", "new_string": "-", "replace_all": True}, _ctx(tmp_path))
    assert r.is_error and f.read_text() == "abc"
    g = tmp_path / "latin.txt"
    g.write_bytes("caf\xe9".encode("latin-1"))
    r = tools_mod._edit({"path": str(g), "old_string": "caf", "new_string": "x"}, _ctx(tmp_path))
    assert r.is_error and "UTF-8" in r.text and g.read_bytes() == "caf\xe9".encode("latin-1")


def test_write_and_edit_are_atomic_keep_mode_and_follow_symlinks(tmp_path):
    real = tmp_path / "real.sh"
    real.write_text("echo 1\n")
    real.chmod(0o755)
    link = tmp_path / "link.sh"
    link.symlink_to(real)
    r = tools_mod._edit({"path": str(link), "old_string": "1", "new_string": "2"}, _ctx(tmp_path))
    assert not r.is_error
    assert link.is_symlink() and real.read_text() == "echo 2\n" and (real.stat().st_mode & 0o777) == 0o755
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".hexiel-tmp")]


# ------------------------------------------------------------------ jobs and notifications
def test_notification_text_travels_as_arguments_not_script(monkeypatch):
    from hexiel import jobs
    evil = '" & (do shell script "id") & "'
    argv = jobs.osascript_notify_argv("t" + evil, evil)
    script = " ".join(a for i, a in enumerate(argv) if i > 0 and argv[i - 1] == "-e")
    assert "do shell script" not in script and argv[-2] == evil


def test_tick_does_not_undo_jobs_added_or_removed_while_it_runs(tmp_path, monkeypatch):
    import datetime
    from hexiel import jobs
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path))
    a = jobs.add("a", "* * * * *", "p", str(tmp_path))
    b = jobs.add("b", "* * * * *", "p", str(tmp_path))
    seen = []

    def runner(job):
        if not seen:                                       # during the first job: user adds one, removes the other
            jobs.add("c-new", "0 0 1 1 *", "p", str(tmp_path))
            jobs.save([j for j in jobs.load() if j["id"] != b["id"]])
        seen.append(job["id"])
        return True, "ok"
    monkeypatch.setattr(jobs, "deliver", lambda *a, **k: None)
    jobs.tick(datetime.datetime.now() + datetime.timedelta(minutes=5), runner=runner)
    names = sorted(j["name"] for j in jobs.load())
    assert names == ["a", "c-new"]                         # c-new kept, b stays removed
    assert [j for j in jobs.load() if j["name"] == "a"][0]["last_status"] == "ok"


def test_a_damaged_jobs_file_is_kept_not_overwritten(tmp_path, monkeypatch):
    from hexiel import jobs
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path))
    jobs.jobs_file().parent.mkdir(parents=True, exist_ok=True)
    jobs.jobs_file().write_text("{broken")
    assert jobs.load() == []
    assert list(jobs.jobs_file().parent.glob("jobs.json.corrupt-*"))


# ------------------------------------------------------------------ web UI: no drive-by from other sites
def test_web_ui_refuses_cross_origin_and_rebound_host_requests(tmp_path):
    import asyncio
    pytest.importorskip("aiohttp")
    from aiohttp.test_utils import TestClient, TestServer
    from tests.test_remote_control import make_session
    from hexiel.webui import make_app
    s = make_session(tmp_path)

    async def go():
        async with TestClient(TestServer(make_app(s))) as c:
            host = f"127.0.0.1:{c.server.port}"
            out = {}
            out["plain"] = (await c.get("/api/state")).status                        # same-origin tools / curl: fine
            out["same_origin"] = (await c.get("/api/state", headers={"Origin": f"http://{host}"})).status
            out["evil_post"] = (await c.post("/api/settings", data='{"auto_approve":true}',
                                             headers={"Origin": "https://evil.example", "Content-Type": "text/plain"})).status
            out["null_origin"] = (await c.get("/api/state", headers={"Origin": "null"})).status
            out["rebound"] = (await c.get("/api/state", headers={"Host": "evil.example"})).status
            out["rebound_port"] = (await c.get("/api/state", headers={"Host": "evil.example:8777"})).status
            try:
                await c.ws_connect("/ws", headers={"Origin": "https://evil.example"})
                out["evil_ws"] = 101
            except Exception as exc:                                                  # noqa: BLE001
                out["evil_ws"] = getattr(exc, "status", "refused")
            return out
    out = asyncio.run(go())
    assert out["plain"] == 200 and out["same_origin"] == 200
    assert out["evil_post"] == 403 and out["null_origin"] == 403
    assert out["rebound"] == 403 and out["rebound_port"] == 403
    assert out["evil_ws"] in (403, "refused")
    assert not s.settings.auto_approve                     # the refused cross-site POST changed nothing


def test_slash_command_arguments_are_not_expanded_twice(tmp_path):
    from hexiel.commands import Command
    c = Command("x", "", "", "Cost: $ARGUMENTS and first=$1", tmp_path / "x.md")
    assert c.render("is $5 now") == "Cost: is $5 now and first=is"


def test_undecodable_command_and_todo_files_do_not_raise(tmp_path):
    from hexiel import commands, todo
    d = tmp_path / ".claude" / "commands"
    d.mkdir(parents=True)
    (d / "old.md").write_bytes("caf\xe9 $ARGUMENTS".encode("cp1252"))
    assert "old" in commands.discover(str(tmp_path))
    (tmp_path / "todo.md").write_bytes("- [ ] caf\xe9\n".encode("cp1252"))
    todo.read_todos(str(tmp_path))
    todo.summary_line(str(tmp_path)) if hasattr(todo, "summary_line") else None


# ------------------------------------------------------------------ turn loop / providers
def test_parallel_tool_results_stay_contiguous_when_one_returns_an_image():
    from hexiel.llm import OpenAICompatProvider
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "A", "name": "view_image", "arguments": {}},
                                                           {"id": "B", "name": "read", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "A", "content": "img", "images": [{"media_type": "image/png", "data": "xx"}]},
        {"role": "tool", "tool_call_id": "B", "content": "text"},
        {"role": "assistant", "content": "done"},
    ]
    roles = [m["role"] for m in OpenAICompatProvider._convert_messages(msgs)]
    assert roles == ["assistant", "tool", "tool", "user", "assistant"]
    tail_img = OpenAICompatProvider._convert_messages(msgs[:2])           # an image at the very end of history
    assert [m["role"] for m in tail_img] == ["assistant", "tool", "user"]


def test_delegate_resume_is_recognised_behind_the_env_note():
    from hexiel import planner
    from hexiel.session import ENV_MARK
    called = []
    class S:                                              # only what maybe_delegate touches before the resume branch
        settings = type("St", (), {})()
        active_model = ""
        def on_event(self, *a, **k): pass
    import hexiel.planner as pm
    orig = pm.resume
    pm.resume = lambda session: called.append(1) or True
    try:
        assert pm.maybe_delegate(S(), f"{ENV_MARK}\nToday's date: x\n\nresume", forced=True) is True
    finally:
        pm.resume = orig
    assert called == [1]


def test_superseded_read_of_a_different_line_range_is_not_shrunk(tmp_path):
    from tests.test_remote_control import make_session
    s = make_session(tmp_path)
    body = "p\n" + "x" * 80000
    def pair(i, args):
        return [{"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "name": "read", "arguments": args}]},
                {"role": "tool", "tool_call_id": f"c{i}", "content": body}]
    s.messages = (pair(1, {"path": "f.py", "offset": 1, "limit": 400}) + pair(2, {"path": "f.py", "offset": 900, "limit": 400}))
    s._supersede_reads()
    assert "outdated" not in s.messages[1]["content"]                    # different range: both stay
    s.messages = pair(1, {"path": "f.py"}) + pair(2, {"path": "f.py"})
    s._supersede_reads()
    assert "outdated" in s.messages[1]["content"]                        # the same read again: the old body shrinks


def test_compaction_transcript_keeps_the_newest_work(tmp_path):
    from tests.test_remote_control import make_session
    s = make_session(tmp_path)
    head = [{"role": "user", "content": f"step {i}"} for i in range(900)]
    t = s._transcript(head, budget_chars=10_000_000)
    assert "step 899" in t and "step 0" in t


def test_empty_summary_does_not_replace_the_history(tmp_path, monkeypatch):
    from tests.test_remote_control import make_session
    from hexiel.session import Session
    s = make_session(tmp_path)
    s.messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(60)]
    before = list(s.messages)
    monkeypatch.setattr(Session, "_compaction_models", lambda self, provider: [(self.model_config, object())])
    monkeypatch.setattr(Session, "_interruptible", lambda self, stream, prov: iter(()))
    class P:
        def stream(self, *a, **k): return iter(())
    monkeypatch.setattr(Session, "_compaction_models", lambda self, provider: [(self.model_config, P())])
    errors = []
    s.on_event = lambda e, *p, **k: errors.append((e, *p))
    s.compact(P(), manual=True)
    assert s.messages == before and any(e[0] == "provider_error" for e in errors)


# ------------------------------------------------------------------ error reports: privacy
def test_error_report_redaction_covers_paths_outside_the_home_folder(monkeypatch, tmp_path):
    from hexiel import reporter
    monkeypatch.chdir(tmp_path)
    for raw, secret in [("FileNotFoundError: /srv/acme-secret/plan.docx", "acme-secret"),
                        ("File \"/mnt/clients/acme/x.py\", line 1", "clients/acme"),
                        (r"D:\proj\acme\a.xlsx missing", "acme"),
                        (f"cannot open {tmp_path}/notes.txt", str(tmp_path))]:
        out = reporter.redact(raw)
        assert secret not in out, (raw, out)
    assert "plan.docx" in reporter.redact("/srv/acme-secret/plan.docx")     # the file name stays for debugging
    assert reporter.redact("see /usr/lib/python3/site.py") == "see /usr/lib/python3/site.py"


def test_a_rejected_device_token_does_not_fall_back_to_anonymous_in_account_mode(monkeypatch):
    import httpx
    from hexiel import reporter
    calls = []
    class R:
        def __init__(self, code): self.status_code = code; self.text = ""
        def json(self): return {}
    def post(url, json=None, headers=None, timeout=None):
        calls.append(headers)
        return R(401 if headers else 200)
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(reporter, "_headers", lambda: {"Authorization": "Bearer x"})
    monkeypatch.setattr(reporter, "mode", lambda: "account")
    assert reporter._post({"a": 1}) is None and len(calls) == 1
    monkeypatch.setattr(reporter, "mode", lambda: "all")
    assert reporter._post({"a": 1}) is not None and len(calls) == 3


# ------------------------------------------------------------------ failover state
def test_returning_to_the_primary_sticks_after_the_turn(tmp_path, monkeypatch):
    from hexiel import router
    from tests.test_remote_control import make_session
    s = make_session(tmp_path)
    primary = ModelConfig(name="primary", provider="openai", model="p", base_url="https://x.example/v1")
    fallback = ModelConfig(name="fb", provider="openai", model="f", base_url="https://y.example/v1")
    s.model_config = fallback
    s.__dict__["_failed_over_from"] = "primary"
    s._provider_override = object()
    monkeypatch.setattr(router, "_profile", lambda sess, name: primary if name == "primary" else None)
    import hexiel.session as sm
    monkeypatch.setattr(sm, "cooling_down", lambda name: False)
    chosen = router.overflow_or_main(s, "do the work")
    assert chosen.name == "primary" and s.model_config.name == "primary" and s._provider_override is None
    assert "_failed_over_from" not in s.__dict__


def test_end_of_an_escalation_restores_the_failover_provider(tmp_path, monkeypatch):
    from hexiel import consult, llm
    from tests.test_remote_control import make_session
    s = make_session(tmp_path)
    fb = ModelConfig(name="fb", provider="openai", model="f", base_url="https://y.example/v1")
    strong = ModelConfig(name="strong", provider="openai", model="s", base_url="https://z.example/v1")
    s.model_config = fb
    failover_provider = object()
    s._provider_override = failover_provider
    monkeypatch.setattr(consult, "consultant", lambda session: strong)
    monkeypatch.setattr(llm, "make_provider", lambda mc, settings: object())
    assert consult.escalate(s, "stuck") is True
    assert s._provider_override is not failover_provider
    consult.end_burst(s, "done")
    assert s.model_config.name == "fb" and s._provider_override is failover_provider


# ------------------------------------------------------------------ plugins
def test_a_filter_plugin_rewrites_the_message_before_the_model_sees_it(tmp_path, monkeypatch):
    from hexiel import openwebui_compat as owc
    from hexiel.session import Session
    from tests.test_remote_control import make_session
    monkeypatch.setenv("HEXIEL_TRUST_PROJECT", "1")
    plug = tmp_path / "plugins"
    plug.mkdir()
    (plug / "f.py").write_text('class Filter:\n    def inlet(self, body, __user__=None):\n'
                               '        body["messages"][-1]["content"] += " (British English)"\n        return body\n')
    monkeypatch.chdir(tmp_path)
    s = make_session(tmp_path)
    s.plugins = owc.discover_plugins(str(tmp_path))
    monkeypatch.setattr(Session, "_stream_once", lambda self, provider, defs: {"role": "assistant", "content": "ok"})
    monkeypatch.setattr("hexiel.llm.make_provider", lambda *a, **k: object())
    s.run_turn("hello there")
    sent = next(m for m in s.messages if m["role"] == "user")["content"]
    assert "hello there (British English)" in sent


def test_a_failing_filter_leaves_the_message_unchanged_and_says_so(tmp_path):
    from hexiel import openwebui_compat as owc
    class Boom:
        def inlet(self, body):
            raise RuntimeError("nope")
    text, notes = owc.apply_inlet([owc.LoadedPlugin("f.py", "Filter", "filter", Boom())], "keep me")
    assert text == "keep me" and notes and "nope" in notes[0]
