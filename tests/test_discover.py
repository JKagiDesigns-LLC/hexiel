"""Discovery and first-run setup: keys, MCP servers, logins and Ollama found in the usual places on three
OS layouts; API keys only (OAuth logins refused), provenance on every finding, secrets masked, keys stored
0600, review 'no' leaves config untouched, the default profile is the first that answers."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import auth, discover, setup
from hexiel.config import ModelConfig, Settings


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HEXIEL_DISCOVER_HOME", str(h))
    monkeypatch.setenv("HEXIEL_HOME", str(h / ".config" / "hexiel"))
    monkeypatch.setenv("XDG_DATA_HOME", str(h / ".local" / "share"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(h / ".config"))
    for env in discover.KEY_ENVS:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(discover, "scan_keyring", lambda: [])
    monkeypatch.setattr(discover, "scan_ollama", lambda: [])            # this machine's Ollama stays out of it
    monkeypatch.setattr(discover, "_windows", lambda: False)
    import hexiel.config as config
    monkeypatch.setattr(config, "CONFIG_FILE", h / ".config" / "hexiel" / "config.toml")
    return h


def _linux_layout(h: Path) -> None:
    (h / ".zshrc").write_text("alias ll='ls -l'\nexport ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnop\nexport PATH=$PATH\n")
    (h / ".env").write_text("DB_PASSWORD=hunter2\nOPENROUTER_API_KEY=sk-or-v1-0123456789abcdef\n")  # pragma: allowlist secret
    (h / ".claude").mkdir()
    (h / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "x@y"},
                                               "mcpServers": {"ollama-vision": {"command": "uvx", "args": ["ollama-vision"]}}}))
    (h / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-secret"}}))
    (h / ".claude" / "skills" / "docs").mkdir(parents=True)
    (h / ".codex").mkdir()
    (h / ".codex" / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "sk-proj-0123456789abcdefghij",  # pragma: allowlist secret
                                                        "tokens": {"access_token": "eyJ-chatgpt"}}))
    (h / ".codex" / "config.toml").write_text('[mcp_servers.github]\ncommand = "npx"\nargs = ["-y", "@modelcontextprotocol/server-github"]\n')
    (h / ".gemini").mkdir()
    (h / ".gemini" / ".env").write_text("GEMINI_API_KEY=AIzaSy-0123456789abcdefghij\n")  # pragma: allowlist secret
    (h / ".gemini" / "oauth_creds.json").write_text("{}")
    oc = h / ".local" / "share" / "opencode"
    oc.mkdir(parents=True)
    (oc / "auth.json").write_text(json.dumps({"anthropic": {"type": "oauth", "access": "x", "refresh": "y"},
                                              "deepseek": {"type": "api", "key": "sk-deepseek-0123456789abcdef"}}))
    occ = h / ".config" / "opencode"
    occ.mkdir(parents=True)
    (occ / "opencode.json").write_text(json.dumps({
        "mcp": {"fetch": {"type": "local", "command": ["uvx", "mcp-server-fetch"]}},
        "provider": {"gmktec": {"options": {"baseURL": "http://192.168.3.10:11434/v1"},
                                "models": {"qwen3.6:35b-a3b-coding": {}}}}}))


def test_linux_layout_finds_keys_logins_servers_and_refuses_oauth(home):
    _linux_layout(home)
    found = discover.discover(str(home), network=False)
    keys = {f.name: f for f in found if f.kind == "key"}
    assert set(keys) == {"ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY"}
    assert "DB_PASSWORD" not in keys                                        # unknown variables are never taken
    assert keys["ANTHROPIC_API_KEY"].source == "~/.zshrc line 2" and keys["ANTHROPIC_API_KEY"].value == "sk-ant-api03-abcdefghijklmnop"
    assert keys["ANTHROPIC_API_KEY"].masked == "sk-ant-…nop" and "abcdefghij" not in keys["ANTHROPIC_API_KEY"].line()
    logins = {f.name for f in found if f.kind == "login"}
    assert any("claude.ai" in n for n in logins) and any("ChatGPT" in n for n in logins) \
        and any("Google" in n for n in logins) and any("anthropic login" in n for n in logins)
    assert not any("oat01" in f.value for f in found)                       # OAuth tokens are never carried
    mcp = {f.name: json.loads(f.value) for f in found if f.kind == "mcp"}
    assert mcp["ollama-vision"]["command"] == "uvx" and mcp["github"]["args"][1].endswith("server-github")
    assert mcp["fetch"] == {"command": "uvx", "args": ["mcp-server-fetch"], "env": {}}
    assert [f for f in found if f.kind == "skills"][0].data["count"] == 1
    endpoint = [f for f in found if f.kind == "endpoint"][0]
    assert endpoint.data["base_url"] == "http://192.168.3.10:11434/v1"
    text = "\n".join(discover.summary(found))
    assert "API keys:" in text and "Logins hexiel cannot use:" in text and "sk-ant-…nop" in text and "hunter2" not in text


def test_windows_and_mac_layouts(home, monkeypatch):
    # Windows: %APPDATA% for OpenCode/Goose, user variables in the registry are read through scan_environment
    monkeypatch.setattr(discover, "_windows", lambda: True)
    appdata = home / "AppData" / "Roaming"
    (appdata / "opencode").mkdir(parents=True)
    (appdata / "opencode" / "auth.json").write_text(json.dumps({"openai": {"type": "api", "key": "sk-proj-win0123456789abcdef"}}))  # pragma: allowlist secret
    monkeypatch.setenv("APPDATA", str(appdata))
    (appdata / "Block" / "goose" / "config").mkdir(parents=True)
    (appdata / "Block" / "goose" / "config" / "config.yaml").write_text("GOOSE_PROVIDER: anthropic\nGOOSE_MODEL: claude-sonnet-4\nextensions:\n  x: {}\n")
    found = discover.discover(str(home), network=False)
    assert {f.name for f in found if f.kind == "key"} == {"OPENAI_API_KEY"}
    assert any(f.kind == "endpoint" and "Goose uses anthropic" in f.name for f in found)
    # macOS: the same dot-files under $HOME; a fish config exports with set -gx
    monkeypatch.setattr(discover, "_windows", lambda: False)
    (home / ".config" / "fish").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "fish" / "config.fish").write_text("set -gx XAI_API_KEY xai-0123456789abcdefghij\n")
    (home / ".aider.conf.yml").write_text("model: gpt-4o\nopenai-api-key: sk-aider-0123456789abcdef\n")
    found = discover.discover(str(home), network=False)
    keys = {f.name: f.source for f in found if f.kind == "key"}
    assert keys["XAI_API_KEY"] == "~/.config/fish/config.fish line 1" and keys["OPENAI_API_KEY"].startswith("~/.aider.conf.yml")


def test_the_same_key_in_two_places_is_one_finding(home, monkeypatch):
    (home / ".bashrc").write_text("export GEMINI_API_KEY=AIza-same-key-0123456789\n")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-same-key-0123456789")
    found = [f for f in discover.discover(str(home), network=False) if f.kind == "key"]
    assert len(found) == 1 and "the environment" in found[0].source and "~/.bashrc line 1" in found[0].source


def test_setup_stores_keys_0600_adds_profiles_and_picks_a_default(home, monkeypatch):
    _linux_layout(home)
    from hexiel.config import load_config
    load_config()                                                           # the starter config
    probed = []

    def fake_probe(mc, key=""):
        probed.append((mc.name, bool(key)))
        return "ok" if mc.name == "gemini" else "HTTP 401: nope"
    monkeypatch.setattr("hexiel.doctor.probe_model", fake_probe)
    lines = []
    result = setup.run(str(home), ask=lambda _p: "", say=lines.append, interactive=False, network=True)
    text = "\n".join(lines)
    assert "sk-ant-…nop" in text and "abcdefghijklmnop" not in text
    stored = auth.keys()
    assert stored["ANTHROPIC_API_KEY"] == "sk-ant-api03-abcdefghijklmnop" and stored["DEEPSEEK_API_KEY"].startswith("sk-deepseek")  # pragma: allowlist secret
    assert "oat01" not in auth.auth_path().read_text() and "chatgpt" not in auth.auth_path().read_text().lower()
    assert stat.S_IMODE(auth.auth_path().stat().st_mode) == 0o600
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-api03-abcdefghijklmnop"   # exported for this process  # pragma: allowlist secret
    cfg = load_config()
    assert cfg.default_model == "gemini" and result["default"] == "gemini"      # the cheapest profile that answered
    assert "✓ default model: 'gemini'" in text
    mcp = json.loads((home / ".config" / "hexiel" / "mcp.json").read_text())["mcpServers"]
    assert set(mcp) >= {"ollama-vision", "github", "fetch"}
    assert "Ollama (free, local models" in text                                 # what is still missing, with advice
    assert not any(name == "anthropic" and ok is False for name, ok in probed)   # keyed profiles are probed with their key


def test_saying_no_leaves_config_and_keys_untouched(home, monkeypatch):
    _linux_layout(home)
    from hexiel.config import load_config
    load_config()
    before = (home / ".config" / "hexiel" / "config.toml").read_text()
    monkeypatch.setattr("hexiel.doctor.probe_model", lambda mc, key="": "HTTP 401: nope")
    lines = []
    setup.run(str(home), ask=lambda _p: "n", say=lines.append, interactive=True, network=True)
    assert auth.keys() == {} and not (home / ".config" / "hexiel" / "mcp.json").exists()
    assert (home / ".config" / "hexiel" / "config.toml").read_text() == before
    assert "✗ no model can answer yet" in "\n".join(lines)


def test_a_loaded_local_model_wins_and_gets_a_profile(home, monkeypatch):
    from hexiel.config import load_config
    load_config()
    ollama = discover.Finding("ollama", "Ollama running", "http://localhost:11434", "Ollama",
                              data={"reachable": True, "base_url": "http://localhost:11434", "local": ["qwen3.6:35b"],
                                    "cloud": [], "loaded": ["qwen3.6:35b"]})
    monkeypatch.setattr(discover, "discover", lambda cwd, network=True: [ollama])
    monkeypatch.setattr("hexiel.doctor.probe_model", lambda mc, key="": "ok")
    lines = []
    result = setup.run(str(home), ask=lambda _p: "", say=lines.append, interactive=False, network=True)
    cfg = load_config()
    assert result["default"] == "qwen3-6" and cfg.models["qwen3-6"].model == "qwen3.6:35b" and cfg.default_model == "qwen3-6"
    assert "pulled on your Ollama (profile added)" in "\n".join(lines)


def test_auth_cli_set_list_remove_and_startup_export(home, monkeypatch, capsys):
    from hexiel.config import load_config
    load_config()
    assert auth.cli(["set", "anthropic", "sk-ant-api03-zzzzzzzzzzzzzzzz"]) == 0
    assert "stored ANTHROPIC_API_KEY = sk-ant-…zzz" in capsys.readouterr().out
    assert auth.cli(["list"]) == 0 and "sk-ant-…zzz" in capsys.readouterr().out
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert auth.keys_into_env() == ["ANTHROPIC_API_KEY"] and os.environ["ANTHROPIC_API_KEY"].endswith("zzzz")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "shell-wins")
    assert auth.keys_into_env() == [] and os.environ["ANTHROPIC_API_KEY"] == "shell-wins"  # pragma: allowlist secret
    assert auth.cli(["remove", "ANTHROPIC_API_KEY"]) == 0 and auth.keys() == {}
    with pytest.raises(SystemExit):
        auth.cli(["set", "no-such-profile", "x"])
