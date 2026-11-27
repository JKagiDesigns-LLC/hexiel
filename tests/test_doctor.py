"""doctor, :checkhealth style: grouped levels, advice under every non-OK line, a clean install without
errors, the one-request model probe, --fix and --json."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import doctor
from hexiel.config import ModelConfig


@pytest.fixture
def quiet(monkeypatch, tmp_path):
    """No network, no real PATH surprises: a fresh home with a starter config and a reachable fake server."""
    import hexiel.config as config
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.toml")
    monkeypatch.setattr(doctor, "CONFIG_FILE", tmp_path / "config.toml")
    monkeypatch.setattr(config, "list_ollama_models", lambda base_url="": ["glm-5.3-flash:cloud", "qwen3.6:35b"])
    monkeypatch.setattr(doctor, "probe_model", lambda mc, key="": "ok")
    monkeypatch.setattr(doctor, "ollama_runtime_checks", lambda mc: [])
    monkeypatch.setattr(doctor, "load_token", lambda: "")
    import hexiel.skills as skills
    monkeypatch.setattr(skills, "discover", lambda cwd: [])
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    import collections
    usage = collections.namedtuple("usage", "total used free")      # a full disk on the test machine is not a doctor failure
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda p: usage(500e9, 100e9, 400e9))
    return tmp_path


def _by_name(items):
    return {c.name: c for c in items}


def test_a_fresh_install_has_no_errors_and_groups_its_lines(quiet, capsys):
    items = doctor.checks(str(quiet))
    by = _by_name(items)
    assert [c.level for c in items if c.level == doctor.ERROR] == []
    assert by["config"].level == doctor.INFO and "starter config" in by["config"].detail   # not a failure
    assert by["memory"].level == doctor.INFO and by["memory"].fix is not None
    assert by["skills"].level == doctor.INFO and by["gitlab"].level == doctor.INFO
    assert by["answers"].level == doctor.OK
    text = doctor.render(items, colour=False)
    assert text.startswith("hexiel ") and "## Runtime" in text and "## Models" in text and "## Project" in text
    assert text.index("## Runtime") < text.index("## Models") < text.index("## Tools") < text.index("## Project")
    assert "- INFO no config.toml yet" in text and "  - ADVICE: " in text
    assert "all good — hexiel is ready" in doctor.summary(items)
    assert doctor.doctor_main(str(quiet)) == 0
    assert "## Integrations" in capsys.readouterr().out


def test_every_non_ok_line_carries_advice_or_a_fix(quiet):
    for c in doctor.checks(str(quiet)):
        if c.level in (doctor.WARNING, doctor.ERROR):
            assert c.advice or c.fix, c.name


def test_the_default_model_is_probed_and_the_failure_says_what_to_do(quiet, monkeypatch):
    monkeypatch.setattr(doctor, "probe_model", lambda mc, key="": "unauthorized")
    by = _by_name(doctor.checks(str(quiet)))
    assert by["answers"].level == doctor.ERROR and "ollama signin" in " ".join(by["answers"].advice)
    assert doctor.doctor_main(str(quiet)) == 1
    import hexiel.config as config
    monkeypatch.setattr(config, "list_ollama_models", lambda base_url="": [])
    by = _by_name(doctor.checks(str(quiet)))
    assert by["provider"].level == doctor.ERROR and "ollama serve" in " ".join(by["provider"].advice)
    monkeypatch.setattr(config, "list_ollama_models", lambda base_url="": ["other:7b"])
    monkeypatch.setattr(doctor, "load_config", lambda: _cfg(ModelConfig(name="loc", provider="openai", model="qwen3.6:35b",
                                                                        base_url="http://localhost:11434/v1")))
    by = _by_name(doctor.checks(str(quiet)))
    assert by["provider"].level == doctor.ERROR and by["provider"].fix_label == "ollama pull qwen3.6:35b"


def _cfg(mc):
    from hexiel.config import Settings

    class Cfg:
        default_model = mc.name
        models = {mc.name: mc}
        settings = Settings()
    return Cfg()


def test_a_missing_cloud_key_is_an_error_only_for_the_default_profile(quiet, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cloud = ModelConfig(name="anthropic", provider="anthropic", model="claude-sonnet-5-5", api_key_env="ANTHROPIC_API_KEY")
    monkeypatch.setattr(doctor, "load_config", lambda: _cfg(cloud))
    by = _by_name(doctor.checks(str(quiet)))
    assert by["provider"].level == doctor.ERROR and "export ANTHROPIC_API_KEY" in by["provider"].advice[0]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-abcdefghijklmnop")
    by = _by_name(doctor.checks(str(quiet)))
    assert by["provider"].level == doctor.OK and by["provider"].detail == "sk-ant-…nop"       # masked


def test_run_checks_keeps_the_old_shape_and_fix_and_json_work(quiet, monkeypatch, capsys):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    res = doctor.run_checks(str(quiet))
    assert res["path"] is False and res["memory"] is True                  # WARNING is not ok; INFO is
    items = doctor.checks(str(quiet))
    done = doctor.apply_fixes(items)
    assert any("created" in d for d in done) and (doctor.home_data_dir() / "memory").is_dir()
    capsys.readouterr()
    assert doctor.doctor_main(str(quiet), as_json_out=True) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["checks"] and {"group", "name", "level", "label", "advice", "fixable"} <= set(data["checks"][0])
    assert data["errors"] == 0 and data["version"]
