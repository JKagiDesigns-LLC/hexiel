"""Hand-edited config.toml mistakes give a readable message, not a Python traceback."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hexiel.config as config

GOOD = '[models.x]\nprovider = "openai"\nmodel = "m"\nbase_url = "http://h/v1"\n'


def load(tmp_path, monkeypatch, text, **kw):
    path = tmp_path / "config.toml"
    path.write_text(text)
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    return config.load_config(**kw)


@pytest.mark.parametrize("text, fragment", [
    ('[models.x]\nprovider = "openai"\nmodel = "m"\nbogus = 1\n', "unknown setting(s): bogus"),
    ('[models.x]\nmodel = "m"\n', "needs provider"),
    ('[models.x]\nprovider = "openai"\n', "needs model"),
    ('[models]\nx = 3\n', "must be a table"),
    (GOOD + '[settings]\ncompact_threshold = "high"\n', "wrong type or format"),
])
def test_bad_values_are_explained(tmp_path, monkeypatch, text, fragment):
    with pytest.raises(SystemExit) as exc:
        load(tmp_path, monkeypatch, text)
    assert "bad config" in str(exc.value) and fragment in str(exc.value)


def test_default_must_name_a_real_profile(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match=r"default = 'nope' is not one of your \[models\.\*\] profiles \(you have: x\)"):
        load(tmp_path, monkeypatch, 'default = "nope"\n' + GOOD)
    with pytest.raises(ValueError, match=r"--model 'ghost' is not one of your"):        # callers fall back on this
        load(tmp_path, monkeypatch, GOOD, model_override="ghost")


def test_valid_config_still_loads_and_delegate_settings_are_normalised(tmp_path, monkeypatch):
    cfg = load(tmp_path, monkeypatch, 'default = "x"\n' + GOOD + '[settings]\nauto_delegate = "ASK"\ndelegate_retries = -3\n')
    assert cfg.default_model == "x" and cfg.settings.auto_delegate == "ask" and cfg.settings.delegate_retries == 0
    odd = load(tmp_path, monkeypatch, GOOD + '[settings]\nauto_delegate = "sometimes"\n')
    assert odd.settings.auto_delegate == "off"                              # unknown value = the safe default
