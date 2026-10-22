"""Hexiel Remote client: path allow-list, config perms, and a full tunnel round trip against a
minimal in-process relay (the real relay lives in the private hexiel-cloud repo)."""

import asyncio
import base64
import json
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import remote

aiohttp = pytest.importorskip("aiohttp")


def test_only_web_ui_paths_cross_the_tunnel():
    for ok in ("/", "/ws", "/static/app.js", "/api/state?x=1"):
        assert remote.path_allowed(ok)
    for bad in ("/v1/chat/completions", "/etc/passwd", "/static/../secret", "/api/../v1/x", "//evil"):
        assert not remote.path_allowed(bad)


def test_config_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path))
    remote.save_config({"device_token": "hxd_x"})
    assert stat.S_IMODE(remote.config_file().stat().st_mode) == 0o600
    assert remote.load_config()["device_token"] == "hxd_x"


async def _tunnel_round_trip(tmp_path):
    from aiohttp import web
    from hexiel.config import ModelConfig, Settings
    from hexiel.session import Session
    from hexiel.tooling import Toolchain
    from hexiel.tools import ToolContext, build_tools
    from hexiel.webui import make_app

    session = Session(ModelConfig(name="t", provider="openai", model="m", base_url="http://127.0.0.1:9/v1"),
                      Settings(), cwd=str(tmp_path), tools=build_tools(Toolchain()), system_prompt="t")
    session.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    runner = web.AppRunner(make_app(session))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    got = {}

    async def relay_ws(request):          # stands in for the relay: asks the device for two things
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"t": "http", "id": "1", "method": "GET", "path": "/api/state", "headers": {}, "body": ""})
        await ws.send_json({"t": "http", "id": "2", "method": "GET", "path": "/v1/models", "headers": {}, "body": ""})
        async for msg in ws:
            f = json.loads(msg.data)
            got[f.get("id") or f["t"]] = f
            if "1" in got and "2" in got:
                await ws.close()
        return ws

    relay = web.Application()
    relay.router.add_get("/ws/device", relay_ws)
    rrunner = web.AppRunner(relay)
    await rrunner.setup()
    rsite = web.TCPSite(rrunner, "127.0.0.1", 0)
    await rsite.start()
    rport = rsite._server.sockets[0].getsockname()[1]

    tunnel = remote.Tunnel({"relay": f"http://127.0.0.1:{rport}", "device_token": "t", "name": "x"},
                           f"http://127.0.0.1:{port}", str(tmp_path), session)
    task = asyncio.create_task(tunnel.run_forever())
    for _ in range(100):
        if "1" in got and "2" in got:
            break
        await asyncio.sleep(0.05)
    tunnel.stop.set()
    await asyncio.wait_for(task, 5)
    await runner.cleanup()
    await rrunner.cleanup()
    assert got["1"]["status"] == 200 and "status" in json.loads(base64.b64decode(got["1"]["body"]))
    assert got["2"]["status"] == 404          # /v1 never crosses the tunnel


def test_tunnel_round_trip(tmp_path):
    asyncio.run(_tunnel_round_trip(tmp_path))


def test_a_second_process_cannot_hold_the_connection(tmp_path, monkeypatch):
    """The relay keeps only the newest connection per device, so two clients would flap forever."""
    import subprocess
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path))
    assert remote.acquire_single_instance()
    code = "from hexiel import remote; print(remote.acquire_single_instance())"
    other = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           env={**__import__("os").environ, "HEXIEL_DATA": str(tmp_path)})
    assert other.stdout.strip() == "False"


def test_systemd_unit_runs_the_remote_client_in_the_background():
    unit = remote.systemd_unit()
    assert "--serve --remote" in unit and "Restart=always" in unit and "WantedBy=default.target" in unit
    assert f"--port {remote.SERVICE_PORT}" in unit


def test_the_service_works_in_its_own_folder_not_the_home_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path))
    work = remote.workspace_dir()
    assert work.is_dir() and work != Path.home() and f"WorkingDirectory={work}" in remote.systemd_unit()
    assert str(work) == remote._service_command()[-1]
    project = tmp_path / "proj"
    project.mkdir()
    remote.save_config({"device_token": "t", "workdir": str(project)})
    assert remote.workspace_dir() == project


def test_install_needs_a_login_first(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path))
    with pytest.raises(RuntimeError, match="not signed in"):
        remote.install_service()


def test_install_writes_the_unit_and_enables_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(remote.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(remote.sys, "platform", "linux")
    monkeypatch.setattr(remote.shutil, "which", lambda name: "/usr/bin/systemctl")
    calls = []
    monkeypatch.setattr(remote, "_run", lambda cmd: calls.append(cmd) or remote.subprocess.CompletedProcess(cmd, 0, "", ""))
    remote.save_config({"relay": "https://r", "device_id": "d", "device_token": "t"})
    remote.install_service()
    assert (tmp_path / ".config/systemd/user/hexiel-remote.service").is_file()
    assert ["systemctl", "--user", "enable", "--now", "hexiel-remote"] in calls


def test_without_a_console_output_goes_to_a_log_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path))
    monkeypatch.setattr(remote.sys, "stderr", None)
    monkeypatch.setattr(remote.sys, "stdout", None)
    remote.ensure_streams()
    print("hello from a scheduled task")
    remote.sys.stdout.flush()
    assert "hello from a scheduled task" in remote.log_file().read_text()


def test_windows_service_is_a_scheduled_task_without_a_console_window(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path))
    monkeypatch.setattr(remote.sys, "platform", "win32")
    (tmp_path / "pythonw.exe").write_text("")
    monkeypatch.setattr(remote.sys, "executable", str(tmp_path / "python.exe"))
    calls = []
    monkeypatch.setattr(remote, "_run", lambda cmd: calls.append(cmd) or remote.subprocess.CompletedProcess(cmd, 0, "", ""))
    remote.save_config({"relay": "https://r", "device_id": "d", "device_token": "t"})
    remote.install_service()
    create = next(c for c in calls if c[:2] == ["schtasks", "/Create"])
    assert "ONLOGON" in create and "pythonw.exe" in create[-1] and "--serve --remote" in create[-1]
    assert ["schtasks", "/Run", "/TN", "HexielRemote"] in calls


def _profiles(monkeypatch, **models):
    import hexiel.config as config
    cfg = config.Config(default_model="local", models=models, settings=config.Settings())
    monkeypatch.setattr(config, "load_config", lambda *a, **k: cfg)


def _model(key_env=""):
    from hexiel.config import ModelConfig
    return ModelConfig(name="m", provider="anthropic", model="x", api_key_env=key_env)


def test_the_service_gets_the_keys_its_models_need_in_a_private_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path))
    _profiles(monkeypatch, local=_model(), cloud=_model("ANTHROPIC_API_KEY"), other=_model("OPENAI_API_KEY"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("UNRELATED_SECRET", "nope")
    captured, missing = remote.write_service_env()
    assert captured == ["ANTHROPIC_API_KEY"] and missing == ["OPENAI_API_KEY"]
    text = remote.service_env_file().read_text()
    assert "ANTHROPIC_API_KEY=sk-test-123" in text and "UNRELATED_SECRET" not in text
    assert stat.S_IMODE(remote.service_env_file().stat().st_mode) == 0o600


def test_recorded_keys_load_without_overriding_the_real_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path))
    remote.service_env_file().write_text("ANTHROPIC_API_KEY=from-file\nOTHER_KEY=also-from-file\n# comment\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OTHER_KEY", "from-shell")
    remote.load_service_env()
    assert remote.os.environ["ANTHROPIC_API_KEY"] == "from-file" and remote.os.environ["OTHER_KEY"] == "from-shell"  # pragma: allowlist secret
    monkeypatch.delenv("ANTHROPIC_API_KEY")


def test_install_says_which_keys_the_service_got_and_which_are_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(remote.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(remote.sys, "platform", "linux")
    monkeypatch.setattr(remote.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(remote, "_run", lambda cmd: remote.subprocess.CompletedProcess(cmd, 0, "", ""))
    _profiles(monkeypatch, cloud=_model("ANTHROPIC_API_KEY"), other=_model("OPENAI_API_KEY"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    remote.save_config({"relay": "https://r", "device_id": "d", "device_token": "t"})
    msg = remote.install_service()
    assert "gave the service these keys" in msg and "ANTHROPIC_API_KEY" in msg and "sk-test-123" not in msg
    assert "OPENAI_API_KEY" in msg and "can't use those models" in msg
    remote.uninstall_service()
    assert not remote.service_env_file().exists()          # it held API keys


def test_a_fallback_model_without_its_key_is_not_offered(monkeypatch):
    from hexiel import jobs
    import hexiel.config as config
    cfg = config.Config(default_model="local", settings=config.Settings(worker_fallback_profile="glm", planner_profile="cloud"),
                        models={"local": _model(), "glm": _model(), "cloud": _model("ANTHROPIC_API_KEY")})
    monkeypatch.setattr(config, "load_config", lambda *a, **k: cfg)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert jobs._fallbacks({}, None) == ["glm"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert jobs._fallbacks({}, None) == ["glm", "cloud"]


def test_the_machine_reports_its_model_profiles_without_any_key(tmp_path, monkeypatch):
    _profiles(monkeypatch, local=_model(), cloud=_model("ANTHROPIC_API_KEY"), other=_model("OPENAI_API_KEY"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    info = remote.model_profiles()
    assert info["default"] == "local"
    by_name = {p["name"]: p for p in info["profiles"]}
    assert by_name["cloud"]["needs_key"] is False and by_name["other"]["needs_key"] is True and by_name["local"]["key_env"] == ""
    assert "sk-secret-value" not in json.dumps(info)
