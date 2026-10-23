"""updter.ensure_platform_tools: installs clipboard helpers on update, never surprises."""

import sys
import subprocess

from hexiel import updater


def test_noop_when_a_helper_already_exists(monkeypatch):
    calls = []
    monkeypatch.setattr(updater, "_sudo_available", lambda: False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a[0] or [], 0))
    # wl-paste/xclip/pngpaste present (the user's real machines) -> nothing happens
    log = []
    updater.ensure_platform_tools(log)
    assert not calls and log == []


def _patch_env(monkeypatch, which_map):
    import shutil

    def fake_which(name):
        return which_map.get(name)

    monkeypatch.setattr(shutil, "which", fake_which)


def test_installs_wl_clipboard_when_sudo_works(monkeypatch):
    log = []
    calls = []

    _patch_env(monkeypatch, {"apt-get": "/usr/bin/apt-get", "sudo": "/usr/bin/sudo"})
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(updater, "_sudo_available", lambda: True)
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    updater.ensure_platform_tools(log)
    assert calls == [["sudo", "-n", "apt-get", "install", "-y", "wl-clipboard"]]
    assert any("installed the clipboard helpers" in line for line in log)


def test_hints_when_sudo_is_interactive(monkeypatch):
    log = []
    calls = []
    _patch_env(monkeypatch, {"apt-get": "/usr/bin/apt-get", "sudo": "/usr/bin/sudo"})
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(updater, "_sudo_available", lambda: False)
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    updater.ensure_platform_tools(log)
    assert not calls          # nothing reboots a password prompt into the update flow
    assert any("manually" in line for line in log)


def test_updates_apply_view_extra(monkeypatch):
    """pip installs must include the view extra so thumbnails/paste work after an upgrade."""
    cmds = []
    monkeypatch.setattr(updater, "is_git_checkout", lambda: False)   # pip path, no git pulls
    monkeypatch.setattr(updater, "ensure_platform_tools", lambda log: None)
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **k: cmds.append(cmd) or subprocess.CompletedProcess(cmd, 1, "boom"))
    ok, log = updater.apply_update()
    assert not ok and "install failed" in log
    assert "serve,view" in cmds[-1][-1]
