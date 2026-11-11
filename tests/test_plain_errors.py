"""Plain-language errors (0.9.2): a provider reply, a dead server or a crash becomes one sentence with the fix;
the budget hard stop refuses cloud requests past stop_at and falls back; the web Setup/Health endpoints."""

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import explain, llm, spend
from hexiel.config import ModelConfig, Settings
from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, build_tools


def mc(**kw):
    base = dict(name="g", provider="openai", model="glm-5.3-flash:cloud", base_url="http://localhost:11434/v1")
    base.update(kw)
    return ModelConfig(**base)


def test_provider_replies_become_a_sentence_with_the_fix():
    assert "ollama signin" in explain.explain('provider 401: {"error":{"message":"Unauthorized"}}', mc())
    assert "hexiel auth set ANTHROPIC_API_KEY" in explain.explain(          # noqa: allowlist secret
        "anthropic 401: invalid x-api-key",
        mc(provider="anthropic", base_url="", api_key_env="ANTHROPIC_API_KEY"))  # pragma: allowlist secret
    assert "ollama serve" in explain.explain("the connection to the model server failed (ConnectError)", mc())
    assert "check the URL" in explain.explain("ConnectError: all connection attempts failed", mc(base_url="https://api.example.com/v1"))
    assert "ollama pull glm-5.3-flash:cloud" in explain.explain("provider 404: model 'glm-5.3-flash:cloud' not found", mc())
    assert "out of credit" in explain.explain("anthropic 400: Your credit balance is too low", mc(provider="anthropic"))
    assert "rate-limited" in explain.explain("provider 429: rate limit exceeded", mc())
    assert "context window" in explain.explain("provider 400: prompt is too long: 210000 tokens > 200000 maximum", mc())
    assert explain.explain("something odd happened", mc()) == "something odd happened"      # unknown: shown, never hidden
    text = explain.crash(TypeError("x is not y"))
    assert text.startswith("something went wrong inside hexiel (TypeError: x is not y)") and "/heal" in text


def test_a_provider_error_reaches_the_turn_as_plain_language(tmp_path, monkeypatch):
    s = Session(mc(), Settings(auto_resume=False), str(tmp_path), [], "sys")
    s.save = lambda: None
    monkeypatch.setattr(llm, "make_provider", lambda m, settings: object())

    def boom(self, provider, defs):
        raise llm.ProviderError('provider 401: {"error":{"message":"Unauthorized"}}')
    monkeypatch.setattr(Session, "_stream_once", boom)
    events = []
    s.on_event = lambda e, *p, **k: events.append((e, *p))
    s.run_turn("hi")
    end = next(p[0] for e, *p in events if e == "turn_end" and p and isinstance(p[0], dict))
    assert "ollama signin" in end["error"] and "Unauthorized" not in end["error"].split("—")[0] or "401" in end["error"]


def test_the_budget_hard_stop_refuses_cloud_requests_and_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(spend, "SNAPSHOT_SECONDS", 0)
    spend.record("ollama", "glm-5.3-flash:cloud", {"prompt_tokens": 10, "completion_tokens": 10})
    spend._write("spend-remote.json", {"ollama": {"at": time.time(), "since": spend.cycle_start(1).isoformat(),
                                                  "usd": 61.0, "plan_usd": 60.0, "period_from": spend.cycle_start(1).isoformat()}})
    settings = Settings(spend={"ollama": {"limit": 60, "stop_at": 1.0}}, fallback_profile="local")
    assert spend.accounts(settings)["ollama"]["stop_at"] == 1.0
    reason = spend.over_budget(settings, "ollama")
    assert "over its budget" in reason and "$61.00" in reason and "$60.00" in reason
    assert spend.over_budget(Settings(spend={"ollama": {"limit": 60}}), "ollama") == ""        # no stop_at: never refused
    local = mc(name="local", model="qwen3.6:35b", base_url="http://192.168.3.10:11434/v1")
    import hexiel.config as config_mod
    monkeypatch.setattr(config_mod, "load_config", lambda: type("C", (), {"model": staticmethod(lambda n: local),
                                                                           "models": {"local": local}})())
    s = Session(mc(), settings, str(tmp_path), [], "sys")
    s.save = lambda: None
    monkeypatch.setattr(llm, "make_provider", lambda m, settings: type("P", (), {"name": m.name})())
    seen = []

    def fake_once(self, provider, defs):
        seen.append(self.model_config.name)
        return {"role": "assistant", "content": "done"}
    monkeypatch.setattr(Session, "_stream_once", fake_once)
    events = []
    s.on_event = lambda e, *p, **k: events.append((e, *p))
    s.run_turn("hi")
    assert seen == ["local"]                                                   # the cloud model was never asked
    assert any(e == "fallback" and p[1] == "local" for e, *p in events)
    # no fallback at all: the turn ends with the reason, not a request
    s2 = Session(mc(), Settings(spend={"ollama": {"limit": 60, "stop_at": 1.0}}), str(tmp_path), [], "sys")
    s2.save = lambda: None
    seen.clear()
    events2 = []
    s2.on_event = lambda e, *p, **k: events2.append((e, *p))
    s2.run_turn("hi")
    assert seen == []
    end = next(p[0] for e, *p in events2 if e == "turn_end" and p and isinstance(p[0], dict))
    assert "over its budget" in end["error"] and "stop_at" in end["error"]


# ---------------------------------------------------------------- the web Setup page

@pytest.fixture()
def setup_app(monkeypatch, tmp_path):
    """A session whose config, auth file and spend snapshot all live in tmp."""
    import hexiel.auth as auth_mod
    import hexiel.config as config_mod
    import hexiel.spend as spend_mod
    import hexiel.webui as webui_mod
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        'default = "cloudy"\n'
        "[models.cloudy]\n"
        'provider = "openai"\n'
        'base_url = "https://ollama.com/v1"\n'
        'model = "m:cloud"\n'
        'api_key_env = "OLLAMA_API_KEY"\n'   # pragma: allowlist secret
        "[models.local]\n"
        'provider = "openai"\n'
        'base_url = "http://127.0.0.1:11434/v1"\n'
        'model = "q3"\n'
        "[spend.ollama]\n"
        "limit = 60\n")
    monkeypatch.setattr(config_mod, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(webui_mod, "load_config", lambda: config_mod.load_config())
    monkeypatch.setattr(auth_mod, "auth_path", lambda: tmp_path / "auth.toml")
    monkeypatch.setattr(spend_mod, "snapshot", lambda settings: [
        {"account": "ollama", "usd": 12.0, "limit": 60.0, "renews": __import__("datetime").date(2026, 12, 2)}])
    tools = build_tools(Toolchain())
    session = Session(mc(), Settings(spend={"ollama": {"limit": 60}}), str(tmp_path), tools, "sys")
    session.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    return webui_mod.make_app(session), cfg_file


async def _setup_roundtrip(setup_app, aiohttp):
    app, cfg_file = setup_app
    from aiohttp import web
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    out = {}
    try:
        async with aiohttp.ClientSession() as client:
            out["setup"] = await (await client.get(f"http://127.0.0.1:{port}/api/setup")).json()
            out["budget"] = await (await client.post(f"http://127.0.0.1:{port}/api/setup/budget",
                                                     json={"account": "ollama", "limit": 50, "stop_at": 1.0})).json()
            out["budget_bad"] = await (await client.post(f"http://127.0.0.1:{port}/api/setup/budget",
                                                         json={"account": "../x", "limit": 1})).json()
            out["auth"] = await (await client.post(f"http://127.0.0.1:{port}/api/auth",
                                                   json={"env": "OLLAMA_API_KEY", "value": "sk-test-1234567890abcdef"})).json()
            out["auth_bad"] = await (await client.post(f"http://127.0.0.1:{port}/api/auth",
                                                       json={"env": "lower bad", "value": "x"})).json()
            out["setup2"] = await (await client.get(f"http://127.0.0.1:{port}/api/setup")).json()
    finally:
        await runner.cleanup()
    return out, cfg_file


def test_the_setup_page_shows_keys_profiles_budget_and_takes_edits(setup_app):
    import asyncio
    aiohttp = pytest.importorskip("aiohttp")
    (out, cfg_file) = asyncio.run(_setup_roundtrip(setup_app, aiohttp))
    setup = out["setup"]
    envs = {k["env"] for k in setup["keys"]}
    assert {"OLLAMA_API_KEY"} <= envs                              # the config's own env vars are listed
    assert any(k["env"] == "OLLAMA_API_KEY" and not k["set"] for k in setup["keys"])
    profiles = {p["name"]: p for p in setup["profiles"]}
    assert profiles["cloudy"]["model"] == "m:cloud" and not profiles["cloudy"]["ready"]
    assert profiles["local"]["ready"]                              # needs no key at all
    assert setup["default"] == "cloudy"
    assert setup["budget"] == [{"account": "ollama", "limit": 60, "stop_at": 0, "label": "renews Dec 02"}]
    # the edits landed: config.toml rewritten, the bad account refused, the key stored + shown
    assert "saved to" in out["budget"]["message"] and out["budget_bad"].get("error")
    assert "auth.toml" in out["auth"]["message"] and out["auth_bad"].get("error")
    text = cfg_file.read_text()
    assert "limit = 50" in text and "stop_at = 1" in text
    keys2 = {k["env"]: k for k in out["setup2"]["keys"]}
    assert keys2["OLLAMA_API_KEY"]["set"] and "sk-t" in keys2["OLLAMA_API_KEY"]["masked"]
    assert keys2["OLLAMA_API_KEY"]["source"] == "auth.toml"
    assert profiles_ready(out["setup2"]["profiles"], "cloudy")


def profiles_ready(profiles, name):
    return next(p for p in profiles if p["name"] == name)["ready"]


def test_overloaded_is_not_called_a_rate_limit():
    from hexiel import explain
    from hexiel.config import ModelConfig
    mc = ModelConfig(name="s", provider="anthropic", model="claude-sonnet-5-5", api_key_env="ANTHROPIC_API_KEY")
    said = explain.explain("HTTP 529: Overloaded", mc)
    assert "overloaded" in said and "rate-limited" not in said
    assert "rate-limited" in explain.explain("HTTP 429: rate limit exceeded", mc)
