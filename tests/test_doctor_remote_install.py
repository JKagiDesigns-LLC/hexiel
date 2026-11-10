"""Wizard remote-service opt-in path: install_service() is called when accepted, skipped when declined."""

import sys
from io import StringIO
from pathlib import Path

import pytest

from hexiel import remote
from hexiel.doctor import wizard


def _fake_install(calls):
    def install(workdir=None):
        calls.append("install")
        return "installed (mock)"
    return install


def _prep(monkeypatch, tmp_home, token=True, tty=True, answer="y"):
    """Set up config dirs, fake tty/input, and optionally sign in."""
    monkeypatch.setattr(remote.Path, "home", classmethod(lambda cls: tmp_home))
    monkeypatch.setattr(remote.sys, "platform", "linux")
    monkeypatch.setattr(remote.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(remote, "_run", lambda cmd: remote.subprocess.CompletedProcess(cmd, 0, "", ""))

    # Ensure wizard writes config under the temp home.
    import hexiel.config as config
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_home / ".config" / "hexiel" / "config.toml")

    # Mark signed in if requested.
    if token:
        remote.save_config({"device_token": "hxd_test_device_token", "relay": "https://remote.hexiel.tech"})

    # Pretend we are on a TTY with a fixed answer.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: tty)
    monkeypatch.setattr(sys.stdin, "readline", lambda: answer + "\n")
    # input() uses readline() in CPython; this works around the need for a real terminal.
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)


def test_wizard_installs_remote_service_when_accepted(monkeypatch, tmp_path):
    home = tmp_path / "home"
    calls = []
    _prep(monkeypatch, home, token=True, tty=True, answer="y")
    monkeypatch.setattr(remote, "install_service", _fake_install(calls))

    out = StringIO()
    monkeypatch.setattr(sys, "stdout", out)

    wizard(str(tmp_path / "project"))
    assert calls == ["install"]


def test_wizard_skips_remote_service_when_declined(monkeypatch, tmp_path):
    home = tmp_path / "home"
    calls = []
    _prep(monkeypatch, home, token=True, tty=True, answer="n")
    monkeypatch.setattr(remote, "install_service", _fake_install(calls))

    out = StringIO()
    monkeypatch.setattr(sys, "stdout", out)

    wizard(str(tmp_path / "project"))
    assert calls == []


def test_wizard_does_not_install_when_not_signed_in(monkeypatch, tmp_path):
    home = tmp_path / "home"
    calls = []
    _prep(monkeypatch, home, token=False, tty=True, answer="y")
    monkeypatch.setattr(remote, "install_service", _fake_install(calls))

    out = StringIO()
    monkeypatch.setattr(sys, "stdout", out)

    wizard(str(tmp_path / "project"))
    assert calls == []
