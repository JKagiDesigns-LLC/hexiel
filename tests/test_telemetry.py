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
