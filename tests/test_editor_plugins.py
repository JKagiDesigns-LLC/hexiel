"""The editor plugins (editors/): their ACP clients are run against a fake agent, so no model or editor UI is needed."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EDITORS = ROOT / "editors"


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_vscode_client_speaks_acp_with_a_fake_agent():
    done = subprocess.run(["node", "test.js"], cwd=EDITORS / "vscode", capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ok: acp-client" in done.stdout


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_vscode_extension_parses_and_declares_its_commands():
    assert subprocess.run(["node", "--check", "extension.js"], cwd=EDITORS / "vscode").returncode == 0
    manifest = json.loads((EDITORS / "vscode" / "package.json").read_text())
    declared = {c["command"] for c in manifest["contributes"]["commands"]}
    source = (EDITORS / "vscode" / "extension.js").read_text()
    assert declared and all(f"'{c}'" in source for c in declared)     # every command in the manifest is registered


def _vim_with_vim9():
    vim = shutil.which("vim")
    if not vim:
        return None
    out = subprocess.run([vim, "--version"], capture_output=True, text=True).stdout
    return vim if "VIM - Vi IMproved 9" in out and "+job" in out and "+channel" in out else None


@pytest.mark.skipif(_vim_with_vim9() is None, reason="Vim 9 with +job is not installed")
def test_vim_plugin_chats_asks_permission_and_shows_a_diff():
    done = subprocess.run([_vim_with_vim9(), "-u", "NONE", "-N", "-es", "-S", "editors/test/vim_test.vim"],
                          cwd=ROOT, capture_output=True, text=True, timeout=90)
    assert "ok: vim plugin" in done.stdout, done.stdout + done.stderr


@pytest.mark.skipif(not shutil.which("lua"), reason="lua is not installed")
def test_neovim_plugin_is_valid_lua():
    for name in ("lua/hexiel/init.lua", "plugin/hexiel.lua"):
        assert subprocess.run(["lua", "-e", f'assert(loadfile("{name}"))'], cwd=EDITORS / "nvim").returncode == 0
