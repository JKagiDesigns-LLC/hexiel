"""Anonymous install/upgrade telemetry pings."""

import json
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from hexiel import __version__, telemetry


def test_first_run_sends_install(monkeypatch, tmp_path):
    state = tmp_path / "telemetry.json"
    monkeypatch.setattr(telemetry, "STORAGE", state)
    monkeypatch.setattr(telemetry, "_machine_fingerprint", lambda: "hashed-id")

    sent = []
    def fake_post(url, json=None, timeout=None):
        sent.append(json)
        return httpx.Response(200)
    monkeypatch.setattr(httpx, "post", fake_post)

    telemetry.ping()
    assert len(sent) == 1
    assert sent[0]["event"] == "install"
    assert sent[0]["version"] == __version__
    assert sent[0]["machine_id"] == "hashed-id"
    assert state.exists()


def test_same_version_is_silent(monkeypatch, tmp_path):
    state = tmp_path / "telemetry.json"
    state.write_text(json.dumps({"version": __version__, "last_event": "install", "last_ping": time.time()}))
    monkeypatch.setattr(telemetry, "STORAGE", state)

    sent = []
    monkeypatch.setattr(httpx, "post", lambda **kw: sent.append(kw) or httpx.Response(200))
    telemetry.ping()
    assert not sent


def test_version_change_sends_upgrade(monkeypatch, tmp_path):
    state = tmp_path / "telemetry.json"
    state.write_text(json.dumps({"version": "0.8.0", "last_event": "install", "last_ping": time.time()}))
    monkeypatch.setattr(telemetry, "STORAGE", state)

    sent = []
    def fake_post(url, json=None, timeout=None):
        sent.append(json)
        return httpx.Response(200)
    monkeypatch.setattr(httpx, "post", fake_post)

    telemetry.ping()
    assert len(sent) == 1
    assert sent[0]["event"] == "upgrade"


def test_ping_failure_does_not_raise(monkeypatch, tmp_path):
    state = tmp_path / "telemetry.json"
    monkeypatch.setattr(telemetry, "STORAGE", state)

    def boom(**kw):
        raise httpx.ConnectError("no network")
    monkeypatch.setattr(httpx, "post", boom)

    telemetry.ping()  # should not raise


def test_machine_fingerprint_is_hashed():
    fp = telemetry._machine_fingerprint()
    assert len(fp) == 32
    assert all(c in "0123456789abcdef" for c in fp)


def _setup(monkeypatch, tmp_path, status):
    monkeypatch.setattr(telemetry, "STORAGE", tmp_path / "telemetry.json")
    monkeypatch.setattr(telemetry, "_machine_fingerprint", lambda: "id")
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("HEXIEL_TELEMETRY", raising=False)
    sent = []
    monkeypatch.setattr(httpx, "post", lambda url, json=None, timeout=None: sent.append(json) or httpx.Response(status))
    return sent


@pytest.mark.parametrize("status", [404, 500, 503])
def test_server_error_is_not_recorded_as_sent(monkeypatch, tmp_path, status):
    sent = _setup(monkeypatch, tmp_path, status)
    telemetry.ping()
    assert len(sent) == 1
    assert telemetry._event_kind() == "install"          # still pending


def test_failed_ping_is_not_retried_every_launch(monkeypatch, tmp_path):
    sent = _setup(monkeypatch, tmp_path, 500)
    telemetry.ping()
    telemetry.ping()
    assert len(sent) == 1
    monkeypatch.setattr(telemetry, "RETRY_AFTER", 0.0)
    telemetry.ping()
    assert len(sent) == 2


def test_opt_out(monkeypatch, tmp_path):
    sent = _setup(monkeypatch, tmp_path, 200)
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    telemetry.ping()
    monkeypatch.delenv("DO_NOT_TRACK")
    monkeypatch.setenv("HEXIEL_TELEMETRY", "off")
    telemetry.ping()
    assert not sent


def test_background_ping_does_not_block(monkeypatch, tmp_path):
    import threading
    gate = threading.Event()
    monkeypatch.setattr(telemetry, "STORAGE", tmp_path / "t.json")
    monkeypatch.setattr(telemetry, "_machine_fingerprint", lambda: "id")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: gate.wait(5) or httpx.Response(200))
    t0 = time.time()
    telemetry.ping_in_background()
    assert time.time() - t0 < 1
    gate.set()
