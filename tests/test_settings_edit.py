"""Runtime settings editing: session-only vs permanent, comment-preserving TOML edits."""

import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import config as config_mod
from hexiel import settings_edit as SE

CFG = '''# my hexiel config
default = "local"   # boot profile

[settings]
auto_approve = false        # careful!
compact_threshold = 0.82

[models.local]
provider = "openai"
base_url = ""
model = "qwen-a"   # the good one
context_window = 65536
extra_body = { reasoning_effort = "none" }

[models.cloud]
provider = "openai"
model = "big-b"
'''


@pytest.fixture
def session(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(CFG)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", path)
    from hexiel.session import Session
    cfg = config_mod.load_config()
    return Session(cfg.model(), cfg.settings, str(tmp_path), [], "sys")


def test_rows_show_live_and_saved(session):
    rows = {r["key"]: r for r in SE.rows(session)}
    assert rows["profile"]["value"] == "local"
    assert rows["model.reasoning"]["value"] == "none"
    assert not any(r["session_only"] for r in rows.values())


def test_session_scope_leaves_file_alone(session):
    before = config_mod.CONFIG_FILE.read_text()
    SE.apply(session, "model.context_window", "131072", "session")
    assert session.model_config.context_window == 131072
    assert config_mod.CONFIG_FILE.read_text() == before
    row = next(r for r in SE.rows(session) if r["key"] == "model.context_window")
    assert row["session_only"] and row["saved"] == 65536


def test_permanent_edits_one_line_and_keeps_comments(session):
    SE.apply(session, "model.model", "qwen-c", "permanent")
    text = config_mod.CONFIG_FILE.read_text()
    assert 'model = "qwen-c"   # the good one' in text
    assert text.count("\n") == CFG.count("\n")                 # no lines added/removed
    assert '# careful!' in text and '# boot profile' in text
    assert tomllib.loads(text)["models"]["local"]["model"] == "qwen-c"
    assert tomllib.loads(text)["models"]["cloud"]["model"] == "big-b"  # other profile untouched


def test_permanent_profile_sets_default(session):
    SE.apply(session, "profile", "cloud", "permanent")
    assert session.model_config.name == "cloud"
    assert tomllib.loads(config_mod.CONFIG_FILE.read_text())["default"] == "cloud"


def test_bool_and_missing_key_insert(session):
    SE.apply(session, "settings.auto_approve", "true", "permanent")
    SE.apply(session, "settings.search_backend", "searxng", "permanent")
    parsed = tomllib.loads(config_mod.CONFIG_FILE.read_text())
    assert parsed["settings"]["auto_approve"] is True and session.auto_approve
    assert parsed["settings"]["search_backend"] == "searxng"
    assert parsed["models"]["local"]["model"] == "qwen-a"     # inserted inside [settings]


def test_reasoning_default_removes_extra_body(session):
    SE.apply(session, "model.reasoning", "model default", "permanent")
    parsed = tomllib.loads(config_mod.CONFIG_FILE.read_text())
    assert "extra_body" not in parsed["models"]["local"]
    SE.apply(session, "model.reasoning", "low", "permanent")
    parsed = tomllib.loads(config_mod.CONFIG_FILE.read_text())
    assert parsed["models"]["local"]["extra_body"] == {"reasoning_effort": "low"}


def test_backup_written_once(session):
    SE.apply(session, "model.max_output_tokens", 4096, "permanent")
    SE.apply(session, "model.max_output_tokens", 8192, "permanent")
    backup = config_mod.CONFIG_FILE.with_name("config.toml.bak")
    assert backup.read_text() == CFG


def test_every_preset_is_a_valid_profile():
    from hexiel import presets
    from hexiel.config import ModelConfig
    for name in presets.BY_NAME:
        parsed = tomllib.loads(presets.toml_block(name))["models"][name]
        mc = ModelConfig(name=name, **parsed)          # every key is a real field
        assert mc.model and mc.provider in ("openai", "anthropic")
        assert mc.provider == "anthropic" or mc.base_url.startswith("http")
        assert 0 < mc.max_output_tokens < mc.context_window


def test_add_preset_profile(session):
    from hexiel import presets
    msg = SE.add_profile("deepseek", presets.as_profile("deepseek"))
    assert "added profile 'deepseek'" in msg
    prof = config_mod.load_config().models["deepseek"]
    assert prof.model == "deepseek-v4-pro" and prof.echo_reasoning is True
    with pytest.raises(ValueError):
        SE.add_profile("deepseek", presets.as_profile("deepseek"))


def test_ollama_runtime_checks(monkeypatch):
    from hexiel import doctor
    from hexiel.config import ModelConfig

    class R:
        def __init__(self, data): self._d = data
        def json(self): return self._d
    ps = {"models": [{"name": "qwen", "context_length": 32768, "size": 20_000_000_000, "size_vram": 12_000_000_000}]}
    monkeypatch.setattr("httpx.get", lambda url, timeout=5: R(ps))
    mc = ModelConfig(name="x", provider="openai", base_url="http://box:11434/v1", model="qwen", context_window=65536)
    checks = {label: (ok, fix) for ok, label, detail, fix in doctor.ollama_runtime_checks(mc)}
    assert checks["context window vs server"][0] is False and "32768" in checks["context window vs server"][1]
    assert checks["model on GPU"][0] is False
