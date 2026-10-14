"""The container tool runs commands directly (no shell): shell syntax is refused with a way forward,
and `stdin` feeds text to an exec'd command."""

import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel.tools import ToolContext, _container
from hexiel.tooling import ContainerRuntime, Toolchain


@pytest.fixture
def ctx(tmp_path):
    fake = tmp_path / "fakedocker"
    fake.write_text('#!/bin/sh\necho "args: $*"\n[ "$2" = "-i" ] && echo "stdin: $(cat)"\nexit 0\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    chain = Toolchain()
    chain.container = ContainerRuntime(binary=str(fake))
    return ToolContext(cwd=str(tmp_path), chain=chain)


def test_plain_command_runs(ctx):
    out = _container({"action": "exec", "container": "c1", "cmd": "cat /etc/hostname"}, ctx)
    assert not out.is_error and "args: exec c1 cat /etc/hostname" in out.text


@pytest.mark.parametrize("cmd", ["cat <<'EOF' | docker exec -i c tee /tmp/x", "a && b", "ls > out", "a | b"])
def test_shell_syntax_is_refused_with_guidance(ctx, cmd):
    out = _container({"action": "exec", "container": "c1", "cmd": cmd}, ctx)
    assert out.is_error and "needs a shell" in out.text and "stdin" in out.text
    assert "args:" not in out.text                    # nothing was executed


def test_quoted_script_for_sh_c_is_allowed(ctx):
    out = _container({"action": "exec", "container": "c1", "cmd": "sh -c 'ls | wc -l && echo done'"}, ctx)
    assert not out.is_error and "args: exec c1 sh -c ls | wc -l && echo done" in out.text


def test_stdin_is_passed_to_the_command(ctx):
    out = _container({"action": "exec", "container": "c1", "cmd": "git apply -", "stdin": "--- a/x\n+++ b/x\n"}, ctx)
    assert not out.is_error and "args: exec -i c1 git apply -" in out.text and "stdin: --- a/x" in out.text
