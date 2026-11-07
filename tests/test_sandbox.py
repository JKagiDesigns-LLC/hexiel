"""#40 opt-in sandbox — mode wiring, wrap() per platform (bwrap + seatbelt profiles,
container argv), the run-shell choke point carrying the note, and doctor rows."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hexiel import sandbox as B


def reset(mode="off", image=""):
    B.MODE, B.IMAGE = mode, image


def setup_function(fn):
    reset()


def teardown_function(fn):
    reset()


def test_off_by_default_wraps_nothing(tmp_path):
    reset()
    argv = ["/bin/bash", "-lc", "echo hi"]
    assert B.wrap(str(tmp_path), argv) == (argv, "")


def test_safe_on_linux_builds_bwrap_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(B.sys, "platform", "linux")
    monkeypatch.setattr(B.shutil, "which", lambda n: "/usr/bin/bwrap" if n == "bwrap" else None)
    reset("safe")
    argv, note = B.wrap(str(tmp_path), ["/bin/bash", "-lc", "echo hi"])
    assert argv[0] == "bwrap"
    assert argv[argv.index("--ro-bind") + 1] == "/"
    assert f"--bind {tmp_path} {tmp_path}" in " ".join(argv)
    assert argv[-3:] == ["/bin/bash", "-lc", "echo hi"]
    assert note == ""


def test_safe_on_macos_writes_a_seatbelt_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(B.sys, "platform", "darwin")
    monkeypatch.setattr(B.shutil, "which", lambda n: "/usr/bin/sandbox-exec" if n == "sandbox-exec" else None)
    reset("safe")
    argv, note = B.wrap(str(tmp_path), ["/bin/bash", "-lc", "echo hi"])
    assert argv[0] == "sandbox-exec" and argv[1] == "-f"
    profile = Path(argv[2]).read_text()
    assert "(deny default)" in profile and f'(subpath "{tmp_path}")' in profile
    assert "(allow file-read*)" in profile and "(allow network*)" in profile
    assert argv[3:] == ["/bin/bash", "-lc", "echo hi"]
    assert note == ""


def test_safe_missing_infra_falls_back_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(B.sys, "platform", "linux")
    monkeypatch.setattr(B.shutil, "which", lambda n: None)
    reset("safe")
    argv, note = B.wrap(str(tmp_path), ["/bin/bash"])
    assert argv == ["/bin/bash"] and "bubblewrap is missing" in note


def test_container_mode_needs_an_image_and_a_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(B.shutil, "which", lambda n: "" if n in ("docker", "podman") else None)
    reset("container", image="python:3.12-slim")
    argv, note = B.wrap(str(tmp_path), ["/bin/bash", "-lc", "ls"])
    assert "neither docker nor podman" in note
    monkeypatch.setattr(B.shutil, "which", lambda n: "/usr/bin/docker" if n == "docker" else None)
    reset("container")                                  # no image configured → default base image
    argv, note = B.wrap(str(tmp_path), ["/bin/bash", "-lc", "ls"])
    assert argv[0].endswith("docker") and f"{tmp_path}:/work" in " ".join(argv)
    assert argv[argv.index("-w") + 1] == "/work"


def test_apply_reads_settings():
    class S:
        sandbox = "container"
        sandbox_image = "me/box:latest"
    B.apply(S())
    assert (B.MODE, B.IMAGE) == ("container", "me/box:latest")
    class S2:
        sandbox = "bogus"
        sandbox_image = "x"
    B.apply(S2())
    assert (B.MODE, B.IMAGE) == ("off", "x")


def test_run_shell_chokes_the_wrap_in(tmp_path, monkeypatch):
    """The single choke point: every shell call goes through sandbox.wrap and
    surfaces a fallback note; a clean wrap stays silent."""
    from hexiel.tools import Toolchain, ToolContext
    from hexiel import tools as tools_mod
    from hexiel import sandbox as sandbox_mod
    seen = []

    def fake_wrap(cwd, argv):
        seen.append(argv[0])                            # which binary is being sandboxed
        return ["bwrap-box", *argv], ""                 # a pretend box

    monkeypatch.setattr(sandbox_mod, "wrap", fake_wrap)

    class _R:
        returncode = 0
        stdout = stderr = ""

    monkeypatch.setattr(tools_mod, "run_cancellable",
                        lambda argv, cwd, timeout, stop=None, on_timeout=None, on_start=None: _R())
    ctx = ToolContext(cwd=str(tmp_path), chain=Toolchain())
    out = tools_mod._run_shell({"command": "true"}, ctx, ["/bin/bash", "-lc", "true"], "bash")
    assert seen == ["/bin/bash"]
    assert "bwrap-box /bin/bash" not in out.text        # note only mentions what the user should know; argv is internal
    # a fallback note surfaces on the result
    monkeypatch.setattr(sandbox_mod, "wrap", lambda cwd, argv: (argv, "box missing"))
    out = tools_mod._run_shell({"command": "true"}, ctx, ["/bin/bash", "-lc", "true"], "bash")
    assert "⚠ sandbox: box missing" in out.text
