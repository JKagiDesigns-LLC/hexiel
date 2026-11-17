"""[settings] remote_control = "auto": every window connects by itself; /remote-control auto|manual persists the choice."""
import time

from hexiel import config as config_mod
from hexiel import remote_control, settings_edit
from hexiel.tools import ShellTask, _pump


def _cfg(tmp_path, monkeypatch, text):
    f = tmp_path / "config.toml"
    f.write_text(text)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", f)
    return f


MODEL = '[models.m]\nprovider = "openai"\nmodel = "x"\nbase_url = "http://127.0.0.1:9/v1"\n'


def test_setting_defaults_off_and_reads_auto(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch, MODEL + '[settings]\n')
    assert config_mod.load_config().settings.remote_control == "off"
    _cfg(tmp_path, monkeypatch, MODEL + '[settings]\nremote_control = "auto"\n')
    assert config_mod.load_config().settings.remote_control == "auto"


def test_auto_wanted_needs_the_setting_and_a_sign_in(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch, MODEL + '[settings]\nremote_control = "auto"\n')
    monkeypatch.setattr(remote_control, "signed_in", lambda: False)
    assert not remote_control.auto_wanted()
    monkeypatch.setattr(remote_control, "signed_in", lambda: True)
    assert remote_control.auto_wanted()
    _cfg(tmp_path, monkeypatch, MODEL + '[settings]\nremote_control = "off"\n')
    assert not remote_control.auto_wanted()


def test_it_is_a_choice_in_the_settings_editor():
    field = settings_edit.FIELD_BY_KEY["settings.remote_control"]
    assert field["options"] == ["auto", "off"]


def test_a_task_ends_when_its_command_does_even_if_a_child_holds_the_pipe():
    import subprocess
    proc = subprocess.Popen(["bash", "-c", "sleep 15 & echo up $!"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    task = ShellTask("t", "x", proc, time.time())
    t = time.time()
    _pump(task)
    assert task.done and "up" in task.text() and time.time() - t < 8
    import os
    import signal
    os.kill(int(task.text().split()[-1]), signal.SIGTERM)
