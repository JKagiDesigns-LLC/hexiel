"""Cross-platform shell tools: powershell tool, windows fallbacks, ANSI strip."""

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel.tools import ToolContext, _run_bash, _run_powershell, _run_shell
from hexiel.tooling import Toolchain

HAS_PWSH = shutil.which("pwsh") is not None


def ctx(tmp_path) -> ToolContext:
    return ToolContext(cwd=str(tmp_path), chain=Toolchain())


def test_ansi_codes_are_stripped(tmp_path):
    out = _run_shell({"command": "echo color"}, ctx(tmp_path),
                     ["/bin/bash", "-lc", "printf '\\033[31mred\\033[0m\\n'"], "/bin/bash")
    assert "\x1b[" not in out.text
    assert "red" in out.text


@pytest.mark.skipif(not HAS_PWSH, reason="pwsh not installed")
def test_powershell_runs_and_errors(tmp_path):
    r = _run_powershell({"command": "Write-Output (3 * 4)"}, ctx(tmp_path))
    assert r.text.strip().endswith("12") and not r.is_error
    r2 = _run_powershell({"command": "Get-ChildItem -Path 'C:/definitely/nope' -ErrorAction Stop"},
                         ctx(tmp_path))
    assert r2.is_error and "stderr:" in r2.text
    assert "\x1b[" not in r2.text


@pytest.mark.skipif(HAS_PWSH, reason="pwsh IS installed on this machine")
def test_powershell_missing_gives_install_hint(tmp_path):
    r = _run_powershell({"command": "whoami"}, ctx(tmp_path))
    assert r.is_error and "winget install Microsoft.PowerShell" in r.text


def test_powershell_missing_gives_install_hint_forced(tmp_path, monkeypatch):
    """No pwsh on PATH — tool answers with a copy-paste install hint, not a stack."""
    import hexiel.tools as T
    monkeypatch.setattr(T.shutil, "which", lambda *_a, **_k: None)
    r = _run_powershell({"command": "whoami"}, ctx(tmp_path))
    assert r.is_error and "winget install Microsoft.PowerShell" in r.text


def test_powershell_needs_command(tmp_path, monkeypatch):
    import hexiel.tools as T
    monkeypatch.setattr(T, "_pwsh_argv", lambda: "pwsh")      # the check must not depend on PowerShell being installed
    r = _run_powershell({}, ctx(tmp_path))
    assert r.is_error and "command is required" in r.text


def test_windows_bash_falls_back_to_powershell(tmp_path, monkeypatch):
    """On Windows without bash, the `bash` tool routes through PowerShell."""
    import hexiel.tools as T

    monkeypatch.setattr(T.os, "name", "nt")
    monkeypatch.setattr(T.shutil, "which", lambda *_a, **_k: None)  # no bash, no pwsh
    monkeypatch.setattr(T, "_pwsh_argv", lambda: "C:/Ps/fake-pwsh.exe")
    called = {}

    def fake_run_powershell(args, ctx_):
        called["routed"] = True
        return T.text_result("PS-FALLBACK-OK")

    monkeypatch.setattr(T, "_run_powershell", fake_run_powershell)
    r = _run_bash({"command": "echo hi"}, ctx(tmp_path))
    assert called.get("routed") and r.text == "PS-FALLBACK-OK"


def test_windows_no_shells_at_all_gives_hint(tmp_path, monkeypatch):
    import hexiel.tools as T
    monkeypatch.setattr(T.os, "name", "nt")
    monkeypatch.setattr(T.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(T, "_pwsh_argv", lambda: None)
    r = _run_bash({"command": "echo hi"}, ctx(tmp_path))
    assert r.is_error and "winget install Microsoft.PowerShell" in r.text


def test_windows_with_bash_uses_bash(tmp_path, monkeypatch):
    """A real bash on Windows (WSL/git-bash on PATH) is used directly."""
    import hexiel.tools as T
    monkeypatch.setattr(T.os, "name", "nt")
    monkeypatch.setattr(T.shutil, "which", lambda name: "/usr/bin/bash" if name == "bash" else None)
    called = {}
    monkeypatch.setattr(T, "_run_shell",
                        lambda args, c, argv, label: called.update(argv=argv)
                        or T.text_result("BASH-OK"))
    _run_bash({"command": "echo hi"}, ctx(tmp_path))
    assert called.get("argv", [])[0] == "/bin/bash"