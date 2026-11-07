"""#38 LSP-style diagnostics — parsers for the real checker output shapes, the
read-only `diagnostics` tool, and the note appended to write/edit results."""

import subprocess
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hexiel import diagnostics as D
from hexiel.tools import Toolchain, ToolContext, build_tools, text_result


def make_ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(cwd=str(tmp_path), chain=Toolchain())


# ---------------------------------------------------------------- parsers

def test_go_vet_lines_parse():
    out = D._parse_colon_lines("main.go:12:5: undefined: foo\n"
                               "pkg/calc.go:40: mismatched types string and int\n"
                               "# notes\nnot:a:diag: line\n", "go vet", default_sev="error")
    assert [(d.path, d.line, d.col, d.severity) for d in out] == \
        [("main.go", 12, 5, "error"), ("pkg/calc.go", 40, 0, "error")]
    assert "undefined: foo" in out[0].message


def test_tsc_lines_parse():
    out = D._parse_tsc("src/app.ts:10:3 - error TS2343: this syntax is not allowed\n"
                       "src/app.ts:12:1 - warning TS6133: unused import\n"
                       "garbage line that is not a diagnostic\n", "tsc")
    assert [(d.path, d.line, d.col, d.severity) for d in out] == \
        [("src/app.ts", 10, 3, "error"), ("src/app.ts", 12, 1, "warning")]
    assert "TS2343" not in out[0].message and "not allowed" in out[0].message
    assert out[1].severity == "warning"


def test_pyright_json_parses(tmp_path):
    import json as _json
    f = tmp_path / "m.py"
    f.write_text("x = 1\n")
    proc = subprocess.CompletedProcess([], 0)
    proc.stdout = _json.dumps({"generalDiagnostics": [{
        "file": str(f), "severity": "error", "message": "bad type\nmore detail",
        "range": {"start": {"line": 2, "character": 4}}}]})
    proc.stderr = ""
    real = D._run
    D._run = lambda cmd, cwd, timeout: proc if cmd[:1] == ["pyright"] else real(cmd, cwd, timeout)
    try:
        out = D.check(f, str(tmp_path), timeout=10)
    finally:
        D._run = real
    assert out[0].severity == "error" and out[0].line == 3 and "bad type" in out[0].message


def test_format_diag_caps_and_clean_text(tmp_path):
    diags = [D.Diag(path=str(tmp_path / f"a{i}.py"), line=i + 1, col=1,
                    severity="error", message="boom", tool="pyright") for i in range(60)]
    text = D.format_diag(diags, str(tmp_path))
    assert text.count("boom") == 40 and "(+20 more" in text
    assert D.format_diag([], str(tmp_path)) == "no diagnostics"


def test_post_edit_notes_errors_but_stays_quiet_when_clean(tmp_path, monkeypatch):
    f = tmp_path / "m.go"
    f.write_text("package m\n")
    fake = subprocess.CompletedProcess([], 1)
    fake.stdout = "m.go:3: undefined: thing"
    fake.stderr = ""
    monkeypatch.setattr(D.shutil, "which", lambda name: "/usr/bin/" + name)  # every checker "installed"
    monkeypatch.setattr(D, "_run", lambda cmd, cwd, timeout: fake)
    note = D.post_edit(f, str(tmp_path))
    assert note.startswith("\n🩺") and "m.go:3" in note
    # clean file: no note at all
    fake.stdout = ""
    assert D.post_edit(f, str(tmp_path)) == ""
    # disabled: nothing
    assert D.post_edit(f, str(tmp_path), enabled=False) == ""
    # unknown suffix: not even attempted (no subprocess call, no crash)
    assert D.post_edit(tmp_path / "data.bin", str(tmp_path)) == ""


def test_write_and_edit_surface_diagnostics(tmp_path, monkeypatch):
    from hexiel import config as config_mod
    f = tmp_path / "m.go"
    f.write_text("package m\n")
    fake = subprocess.CompletedProcess([], 1)
    fake.stdout = "m.go:1: undefined: nope"
    fake.stderr = ""
    monkeypatch.setattr(D.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(D, "_run", lambda cmd, cwd, timeout: fake)
    ctx = make_ctx(tmp_path)
    from hexiel import tools as tools_mod
    out = tools_mod._write({"path": str(f), "content": "package m\n"}, ctx)
    assert "updated" in out.text
    assert "🩺" in out.text and "m.go:1" in out.text
    out3 = tools_mod._edit({"path": str(f), "old_string": "package m", "new_string": "package m // x"}, ctx)
    assert "🩺" in out3.text and "m.go:1" in out3.text
    # off: quiet (the user's [settings] decides)
    config_mod.CONFIG_FILE.write_text('[settings]\ndiagnostics = "off"\n[models.m]\nprovider = "openai"\nmodel = "m"\nbase_url = ""\n')
    try:
        out2 = tools_mod._write({"path": str(f), "content": "package m\n"}, ctx)
        assert "🩺" not in out2.text
    finally:
        config_mod.CONFIG_FILE.unlink(missing_ok=True)


def test_diagnostics_tool_reports_and_stays_readonly(tmp_path, monkeypatch):
    tools = [t for t in build_tools(Toolchain()) if t.name == "diagnostics"]
    assert len(tools) == 1 and tools[0].readonly
    target = tmp_path / "d.go"                       # exists: checks run on it
    target.write_text("package d\n")
    fake = subprocess.CompletedProcess([], 1)
    fake.stdout, fake.stderr = "d.go:2: bad var", ""
    monkeypatch.setattr(D.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(D, "_run", lambda cmd, cwd, timeout: fake)
    out = tools[0].run({"path": str(tmp_path / "d.go")}, make_ctx(tmp_path))
    assert "error" in out.text.lower() and "d.go:2" in out.text
    # clean run: the 'no diagnostics' text lists what is around
    fake.stdout, fake.stderr = "", ""
    out = tools[0].run({"path": str(tmp_path / "d.go")}, make_ctx(tmp_path))
    assert "no diagnostics" in out.text and "installed checkers" in out.text
    # missing file
    out = tools[0].run({"path": str(tmp_path / "nope.py")}, make_ctx(tmp_path))
    assert out.is_error and "no such file" in out.text


def test_settings_editor_knows_the_toggle(tmp_path):
    from hexiel import settings_edit
    field = next(f for f in settings_edit.FIELDS if f["key"] == "settings.diagnostics")
    assert field["options"] == ["auto", "off"]
