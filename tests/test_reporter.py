"""Error reports: what is scrubbed, who gets sent what, and that a dead relay never hurts a session."""

import json
import time

import pytest

from hexiel import heal, reporter


@pytest.fixture
def sent(monkeypatch):
    """Reporting on, relay replaced by a list; background sends run inline."""
    monkeypatch.delenv("HEXIEL_NO_REPORTS")
    posted = []
    monkeypatch.setattr(reporter, "_post", lambda report, timeout=8.0: posted.append(report) or {"id": "r1", "count": 1})
    monkeypatch.setattr(reporter, "in_background", lambda fn, *a: fn(*a))
    monkeypatch.setattr(reporter, "mode", lambda: "account")
    return posted


def sign_in(monkeypatch, signed=True):
    monkeypatch.setattr(reporter, "_remote", lambda: {"device_token": "hxd_x", "relay": "https://r"} if signed else {})


def test_scrubs_paths_keys_emails_and_lan_addresses():
    text = ('File "/home/alice/proj/x.py", line 3\nC:\\Users\\Bob\\code\\y.py /Users/carol/z.py '
            "key=sk-abcdefghijklmnop1234 ghp_abcdefghijklmnop12 mail me@corp.com host 192.168.2.40 "
            "Authorization: Bearer abcdefghijklmnop.qrst password: hunter2hunter2")
    out = reporter.redact(text)
    for leaked in ("alice", "Bob", "carol", "sk-abc", "ghp_abc", "me@corp.com", "192.168.2.40", "hunter2", "qrst"):
        assert leaked not in out
    assert "~" in out


def test_the_users_home_directory_is_removed_on_any_platform(monkeypatch, tmp_path):
    monkeypatch.setattr(reporter.Path, "home", classmethod(lambda cls: tmp_path / "someone"))
    assert "someone" not in reporter.redact(f"error in {tmp_path / 'someone' / 'a' / 'b.py'}")


def test_the_same_bug_has_the_same_fingerprint_whatever_the_details():
    a = reporter.fingerprint("turn-crash", "KeyError: 'abc' (id 123)", '  File "/a/x/session.py", line 10, in run')
    b = reporter.fingerprint("turn-crash", "KeyError: 'zzz' (id 987)", '  File "C:\\p\\session.py", line 99, in run')
    assert a == b
    assert a != reporter.fingerprint("turn-crash", "ValueError: bad", '  File "/a/x/session.py", line 10, in run')


def test_context_keeps_keys_never_values():
    out = reporter.scrub_context({"arguments": {"command": "echo secret"}, "user_text": "my password is x", "n": 3})
    assert "secret" not in out and "password" not in out and '"arguments"' in out and '"n": 3' in out


def test_an_incident_from_a_signed_in_machine_is_sent_scrubbed(sent, monkeypatch):
    sign_in(monkeypatch)
    heal.record_incident(".", kind="turn-crash", detail="KeyError: 'x' in /home/dan/p.py",
                         traceback_text='File "/home/dan/p.py", line 1, in f', context={"user_text": "private prompt"})
    (report,) = sent
    assert report["kind"] == "error" and report["source"] == "auto"
    assert "dan" not in json.dumps(report) and "private prompt" not in json.dumps(report)
    assert report["title"].startswith("turn-crash: KeyError") and len(report["fingerprint"]) == 32


def test_anonymous_installs_send_nothing_unless_they_opted_in(sent, monkeypatch):
    sign_in(monkeypatch, signed=False)
    assert reporter.auto_report({"kind": "turn-crash", "detail": "boom"}) is False and sent == []
    monkeypatch.setattr(reporter, "mode", lambda: "all")
    assert reporter.auto_report({"kind": "turn-crash", "detail": "boom"}) is True and len(sent) == 1


def test_off_means_off_even_when_signed_in(sent, monkeypatch):
    sign_in(monkeypatch)
    monkeypatch.setattr(reporter, "mode", lambda: "off")
    assert reporter.auto_report({"kind": "turn-crash", "detail": "boom"}) is False and sent == []


def test_bug_command_always_sends_and_redacts(sent, monkeypatch):
    sign_in(monkeypatch, signed=False)
    msg = reporter.file_report("bug", "crashes opening /home/eve/secret.py with key sk-abcdefghijklmnop99")
    (report,) = sent
    assert report["kind"] == "bug" and report["source"] == "manual" and "eve" not in report["body"]
    assert "sk-abc" not in report["body"] and "r1" in msg and "hexiel.tech/support" in msg
    assert reporter.file_report("bug", "  ").startswith("usage:")


def test_an_unreachable_relay_queues_the_report_and_sends_it_later(monkeypatch, tmp_path):
    monkeypatch.delenv("HEXIEL_NO_REPORTS")
    monkeypatch.setattr(reporter, "_remote", lambda: {})
    monkeypatch.setattr(reporter, "_post", lambda report, timeout=8.0: None)
    assert reporter.send({"kind": "bug", "title": "t"}) is None
    assert len(list(reporter.outbox().glob("*.json"))) == 1
    got = []
    monkeypatch.setattr(reporter, "_post", lambda report, timeout=8.0: got.append(report) or {"id": "x"})
    assert reporter.flush_outbox() == 1 and got[0]["title"] == "t" and not list(reporter.outbox().glob("*.json"))


def test_outbox_is_capped(monkeypatch):
    monkeypatch.setattr(reporter, "MAX_OUTBOX", 3)
    for i in range(6):
        reporter._queue({"title": str(i)})
        time.sleep(0.002)
    assert len(list(reporter.outbox().glob("*.json"))) == 3


def test_a_broken_reporter_never_breaks_recording_an_incident(monkeypatch):
    monkeypatch.setattr(reporter, "auto_report", lambda p: 1 / 0)
    assert heal.record_incident(".", kind="x", detail="d") is not None


def test_my_reports_lists_status_and_resolution(monkeypatch):
    sign_in(monkeypatch)

    class Resp:
        status_code = 200
        def json(self): return {"items": [{"status": "resolved", "kind": "error", "title": "KeyError", "count": 3,
                                           "fixed_in": "0.4.2", "resolution": "Fixed."}]}
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: Resp())
    text = reporter.my_reports_text()
    assert "[resolved] error: KeyError (seen 3×) — fixed in 0.4.2" in text and "Fixed." in text
    sign_in(monkeypatch, signed=False)
    assert "hexiel remote login" in reporter.my_reports_text()
