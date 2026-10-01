"""Self-update: version compare, cached checks, refusal on a dirty checkout."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import updater as U


def test_parse_and_verdict(monkeypatch):
    assert U.parse_version("v0.10.2") == (0, 10, 2)
    assert U.parse_version("latest") is None
    monkeypatch.setattr(U, "__version__", "0.2.6")
    assert U._verdict("0.2.7")["available"]
    assert not U._verdict("0.2.6")["available"]
    assert not U._verdict("0.2.5")["available"]
    assert not U._verdict(None)["available"]
    assert U._verdict("0.10.0")["available"]          # numeric, not string, compare


def test_check_is_cached_and_never_raises(monkeypatch):
    calls = []
    monkeypatch.setattr(U, "__version__", "0.2.6")
    monkeypatch.setattr(U, "latest_release", lambda timeout=5.0: calls.append(1) or "0.3.0")
    assert U.check()["available"] and U.check()["latest"] == "0.3.0"
    assert len(calls) == 1                              # second call served from cache
    assert U.check(force=True)["latest"] == "0.3.0" and len(calls) == 2

    def offline(timeout=5.0):
        raise OSError("no network")
    monkeypatch.setattr(U, "latest_release", offline)
    assert U.check(force=True) == {"current": "0.2.6", "latest": None, "available": False}


def test_refuses_when_checkout_has_local_changes(monkeypatch):
    monkeypatch.setattr(U, "is_git_checkout", lambda: True)
    monkeypatch.setattr(U, "_git", lambda *a: subprocess.CompletedProcess(a, 0, " M hexiel/tools.py\n", ""))
    ran = []
    monkeypatch.setattr(U.subprocess, "run", lambda *a, **k: ran.append(a))
    ok, log = U.apply_update()
    assert not ok and "local changes" in log and "hexiel/tools.py" in log
    assert ran == []                                    # never reached pull or pip


def test_restart_resumes_with_continue(monkeypatch):
    seen = {}
    monkeypatch.setattr(U.sys, "argv", ["hexiel", "--serve", "-c", "--port", "9000"])
    monkeypatch.setattr(U.os, "execv", lambda exe, argv: seen.update(argv=argv))
    U.restart()
    assert seen["argv"][1:4] == ["-m", "hexiel", "--serve"]
    assert seen["argv"].count("-c") == 1 and seen["argv"][-1] == "-c"
