"""LSP support, tested against a tiny fake language server (a Python script speaking JSON-RPC over stdio)."""
import json
import sys
import textwrap
from pathlib import Path

import pytest

from hexiel import lsp

FAKE = textwrap.dedent('''
    import json, sys
    def send(m):
        b = json.dumps(m).encode(); sys.stdout.buffer.write(b"Content-Length: %d\\r\\n\\r\\n" % len(b) + b); sys.stdout.buffer.flush()
    def read():
        n = None
        while True:
            l = sys.stdin.buffer.readline()
            if not l: return None
            l = l.strip()
            if not l: break
            if l.lower().startswith(b"content-length:"): n = int(l.split(b":")[1])
        return json.loads(sys.stdin.buffer.read(n))
    docs = {}
    while True:
        m = read()
        if m is None: break
        meth, i, p = m.get("method"), m.get("id"), m.get("params", {})
        if meth == "initialize": send({"jsonrpc": "2.0", "id": i, "result": {"capabilities": {}}})
        elif meth == "textDocument/didOpen":
            d = p["textDocument"]; docs[d["uri"]] = d["text"]
            send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {"uri": d["uri"],
                  "diagnostics": [{"range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 3}},
                                   "severity": 1, "message": "bad thing\\nsecond line"}]}})
        elif meth == "textDocument/definition":
            send({"jsonrpc": "2.0", "id": i, "result": [{"uri": p["textDocument"]["uri"],
                  "range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 7}}}]})
        elif meth == "textDocument/references":
            u = p["textDocument"]["uri"]
            send({"jsonrpc": "2.0", "id": i, "result": [
                {"uri": u, "range": {"start": {"line": 2, "character": 0}, "end": {"line": 2, "character": 3}}},
                {"uri": u, "range": {"start": {"line": 2, "character": 5}, "end": {"line": 2, "character": 8}}}]})
        elif meth == "textDocument/hover":
            send({"jsonrpc": "2.0", "id": i, "result": {"contents": {"kind": "markdown", "value": "def foo() -> int @%d,%d" % (p["position"]["line"], p["position"]["character"])}}})
        elif meth == "workspace/symbol":
            send({"jsonrpc": "2.0", "id": i, "result": [{"name": "foo", "kind": 12, "location": {"uri": "file:///x/a.py", "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}}}]})
        elif meth == "shutdown": send({"jsonrpc": "2.0", "id": i, "result": None})
        elif meth == "exit": break
''')


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HEXIEL_TRUST_PROJECT", "1")
    fake = tmp_path / "fake_lsp.py"
    fake.write_text(FAKE)
    (tmp_path / "proj").mkdir()
    proj = tmp_path / "proj"
    (proj / "pyproject.toml").write_text("")
    (proj / "a.py").write_text("def foo():\n    xx = 1\n    foo(); foo()\n")
    (proj / ".hexiel").mkdir()
    (proj / ".hexiel" / "lsp.toml").write_text(
        f'[server.pyright]\ncommand = ["{sys.executable}", "{fake}"]\n')
    mgr = lsp.Manager()
    yield proj, mgr
    mgr.shutdown()


def call(proj, mgr, **a):
    return lsp.run_action(a, str(proj), manager=mgr)


def test_registry_builtin_and_override(project):
    proj, _ = project
    reg = lsp.load_registry(str(proj))
    assert {"pyright", "typescript", "gopls", "rust-analyzer", "clangd"} <= set(reg)
    assert reg["pyright"].command[0] == sys.executable          # project file replaced the command ...
    assert ".py" in reg["pyright"].extensions                    # ... and kept the rest
    assert lsp.server_for("x/a.ts", reg).name == "typescript"
    assert lsp.server_for("x/readme.md", reg) is None


def test_disable_and_new_server(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_HOME", str(tmp_path))
    (tmp_path / "lsp.toml").write_text('[server.gopls]\ndisabled = true\n[server.zig]\ncommand=["zls"]\nextensions=[".zig"]\n')
    reg = lsp.load_registry(None)
    assert "gopls" not in reg and reg["zig"].extensions == [".zig"]


def test_find_root(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "go.mod").write_text("")
    (tmp_path / "a" / "b" / "x.go").write_text("")
    assert lsp.find_root(str(tmp_path / "a" / "b" / "x.go"), ["go.mod"]) == str(tmp_path / "a")


def test_definition_references_hover(project):
    proj, mgr = project
    out, err = call(proj, mgr, action="definition", path="a.py", symbol="foo", line=3)
    assert not err and out.startswith("a.py:1  def foo():")
    out, err = call(proj, mgr, action="references", path="a.py", symbol="foo")
    assert not err and "2 reference(s)" in out and "a.py:3" in out
    out, err = call(proj, mgr, action="hover", path="a.py", symbol="foo", line=3)
    assert "def foo() -> int" in out and "@2,4" in out          # second `foo` is not the first: line 3, col 4
    assert len(mgr.running()) == 1                               # one server, reused


def test_diagnostics_and_symbols(project):
    proj, mgr = project
    out, err = call(proj, mgr, action="diagnostics", path="a.py")
    assert not err and "1 diagnostic" in out and "a.py:2  error: bad thing" in out and "second line" not in out
    out, err = call(proj, mgr, action="symbols", path="a.py", query="foo")
    assert not err and "foo" in out


def test_edit_resyncs(project):
    proj, mgr = project
    call(proj, mgr, action="hover", path="a.py", symbol="foo")
    (proj / "a.py").write_text("\n\n\nfoo = 1\n")
    out, _ = call(proj, mgr, action="hover", path="a.py", symbol="foo")
    assert "@3,0" in out


def test_errors_are_readable(project, tmp_path):
    proj, mgr = project
    assert call(proj, mgr, action="nope")[1]
    assert "does not appear" in call(proj, mgr, action="hover", path="a.py", symbol="zzz")[0]
    assert "no such file" in call(proj, mgr, action="hover", path="gone.py", symbol="x")[0]
    (proj / "n.txt").write_text("hi")
    assert "no language server is configured" in call(proj, mgr, action="hover", path="n.txt", symbol="hi")[0]


def test_missing_server_says_how_to_install(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_HOME", str(tmp_path / "h"))
    (tmp_path / "x.go").write_text("package x\n")
    monkeypatch.setattr(lsp, "find_program", lambda c: None)
    out, err = lsp.run_action({"action": "hover", "path": "x.go", "symbol": "x"}, str(tmp_path), manager=lsp.Manager())
    assert err and "gopls is not installed" in out and "go install" in out and "where_is" in out


def test_shutdown_kills_servers(project):
    proj, mgr = project
    call(proj, mgr, action="hover", path="a.py", symbol="foo")
    procs = [c.proc for c in mgr._clients.values()]
    mgr.shutdown()
    for p in procs:
        p.wait(timeout=3)
    assert all(p.poll() is not None for p in procs)


def test_tool_is_registered():
    from hexiel.tools import Toolchain, build_tools
    t = {t.name: t for t in build_tools(Toolchain())}
    assert "lsp" in t and t["lsp"].readonly


# ------------------------------------------------------------------ safety and installing

def test_untrusted_project_file_is_ignored(project, monkeypatch):
    proj, mgr = project
    monkeypatch.delenv("HEXIEL_TRUST_PROJECT")
    monkeypatch.setattr("hexiel.openwebui_compat._read_trusted", lambda: [])
    reg = lsp.load_registry(str(proj))
    assert reg["pyright"].command[0] == "pyright-langserver"          # the repo's own command never loaded
    assert "--trust-project" in lsp.untrusted_project_note(str(proj))
    out, err = lsp.run_action({"action": "hover", "path": "a.py", "symbol": "foo"}, str(proj), manager=mgr)
    assert "--trust-project" in out or "not installed" in out
    assert not mgr.running()                                           # nothing was started


def _spec(tmp_path, cmds, found=False):
    prog = tmp_path / "bin" / "fakels"
    return lsp.ServerSpec("fakels", ["fakels"], [".fk"], install="by hand", install_cmds=cmds), prog


class _Proc:
    def __init__(self, out="done\n", code=0, on_run=None):
        import io
        self.stdout, self.returncode, self.pid = io.StringIO(out), code, 1   # pid 1 is never signalled
        if on_run:
            on_run()
    def poll(self):
        return self.returncode
    def kill(self):
        pass


def test_install_asks_first_and_runs_the_exact_command(tmp_path, monkeypatch):
    monkeypatch.setattr(lsp, "extra_bin_dirs", lambda: [str(tmp_path / "bin")])
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    (tmp_path / "bin").mkdir()
    for tool in ("pm",):
        f = tmp_path / "bin" / tool
        f.write_text("#!/bin/sh\n"); f.chmod(0o755)
    spec, prog = _spec(tmp_path, [["nomanager", "x"], ["pm", "install", "fakels"]])
    asked, ran = [], []

    def popen(argv, **kw):
        ran.append(argv)
        return _Proc(on_run=lambda: (prog.write_text("#!/bin/sh\n"), prog.chmod(0o755)))

    text, err = lsp.install_server(spec, lambda d: asked.append(d) or True, popen=popen)
    assert not err and "installed fakels" in text
    assert asked == [f"install the fakels language server: pm install fakels"]       # first available manager, shown verbatim
    assert ran[0][1:] == ["install", "fakels"] and ran[0][0].endswith("/pm")


def test_install_declined_or_unaskable_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(lsp, "extra_bin_dirs", lambda: [str(tmp_path)])
    f = tmp_path / "pm"; f.write_text("#!/bin/sh\n"); f.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    spec, _ = _spec(tmp_path, [["pm", "i", "x"]])
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run"))
    text, err = lsp.install_server(spec, lambda d: False, popen=boom)
    assert err and "declined" in text
    text, err = lsp.install_server(spec, None, popen=boom)
    assert err and "no way to ask" in text


def test_install_failure_and_missing_manager_are_readable(tmp_path, monkeypatch):
    monkeypatch.setattr(lsp, "extra_bin_dirs", lambda: [str(tmp_path)])
    f = tmp_path / "pm"; f.write_text("#!/bin/sh\n"); f.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    spec, _ = _spec(tmp_path, [["pm", "i", "x"]])
    text, err = lsp.install_server(spec, lambda d: True, popen=lambda *a, **k: _Proc("E: no such package\n", 100))
    assert err and "failed (exit 100)" in text and "no such package" in text
    spec2, _ = _spec(tmp_path, [["nomanager", "x"]])
    text, err = lsp.install_server(spec2, lambda d: True)
    assert err and "nomanager" in text and "by hand" in text
    text, err = lsp.install_server(_spec(tmp_path, [])[0], lambda d: True)
    assert err and "no install command" in text


def test_install_can_be_cancelled(tmp_path, monkeypatch):
    import threading
    monkeypatch.setattr(lsp, "extra_bin_dirs", lambda: [str(tmp_path)])
    f = tmp_path / "pm"; f.write_text("#!/bin/sh\n"); f.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    spec, _ = _spec(tmp_path, [["pm", "i", "x"]])
    stop = threading.Event(); stop.set()
    class Hang(_Proc):
        def poll(self):
            return None
    text, err = lsp.install_server(spec, lambda d: True, stop=stop, popen=lambda *a, **k: Hang())
    assert err and "cancelled" in text


def test_find_program_looks_in_install_folders(tmp_path, monkeypatch):
    d = tmp_path / "gobin"; d.mkdir()
    f = d / "gopls"; f.write_text("#!/bin/sh\n"); f.chmod(0o755)
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(lsp, "extra_bin_dirs", lambda: [str(d)])
    assert lsp.find_program("gopls") == str(f)
    assert lsp.load_registry(None)["gopls"].installed()


def test_install_tool_fails_closed_without_an_approval_gate(tmp_path):
    from hexiel.tools import ToolContext, Toolchain, build_tools
    t = {t.name: t for t in build_tools(Toolchain())}["lsp_install"]
    res = t.run({"server": "gopls"}, ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    assert res.is_error and ("no way to ask" in res.text or "already installed" in res.text)
    res = t.run({"server": "nonsense"}, ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    assert res.is_error and "unknown server" in res.text
