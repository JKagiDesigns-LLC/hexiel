"""First-run setup that ends in a working chat.

    hexiel --setup   (also runs by itself the first time hexiel starts in a terminal with no config yet)
    /import          (inside a session: the same discovery, non-interactive, reported as a note)

1. discover what the machine already has (discover.py): keys, MCP servers, skills, rules, Ollama
2. review: one line per thing hexiel would take, y/n each (defaults are sensible; non-interactive takes them)
3. apply: keys → ~/.config/hexiel/auth.toml (0600), a profile per key that has none, MCP servers →
   ~/.config/hexiel/mcp.json
4. pick the default profile: the first that can answer now — a model loaded on your Ollama, a model pulled
   there, Ollama Cloud if signed in, then the cheapest profile with a working key — proven with one request
5. say what is still missing, one ADVICE line per provider, the same lines the doctor prints
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import auth, discover as discover_mod
from .config import CONFIG_FILE, home_config_dir, load_config

CHEAP_FIRST = ("flash", "haiku", "mini", "nano", "lite", "small", "sonnet", "chat", "gpt", "opus", "pro", "grok")


@dataclass
class Action:
    kind: str                # key | profile | mcp | endpoint
    label: str               # one line for the review table, secrets masked
    apply: Callable          # returns a message
    default: bool = True
    data: dict = field(default_factory=dict)


# ---------------------------------------------------------------- plan

def _preset_profile(provider: str, env: str) -> tuple[str, dict] | None:
    """A ready-made profile for a provider from presets.py (name, fields), or None."""
    from . import presets
    for entry in presets.PRESETS:
        _group, name, _title, fields, _note = entry
        if fields.get("api_key_env") == env:
            return name.lower().replace(" ", "-"), dict(fields)
    base = {"anthropic": ("anthropic", dict(provider="anthropic", model="claude-sonnet-5-5", api_key_env=env,
                                            context_window=200000, max_output_tokens=16384)),
            "openai": ("openai", dict(provider="openai", base_url="https://api.openai.com/v1", model="gpt-5.2",
                                      api_key_env=env, context_window=400000, max_output_tokens=16384)),
            "gemini": ("gemini", dict(provider="openai", base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                                      model="gemini-2.5-flash", api_key_env=env, context_window=1000000, max_output_tokens=16384)),
            "openrouter": ("openrouter", dict(provider="openai", base_url="https://openrouter.ai/api/v1",
                                              model="anthropic/claude-sonnet-4.5", api_key_env=env, context_window=200000,
                                              max_output_tokens=16384)),
            "xai": ("xai", dict(provider="openai", base_url="https://api.x.ai/v1", model="grok-4", api_key_env=env,
                                context_window=200000, max_output_tokens=16384)),
            "deepseek": ("deepseek", dict(provider="openai", base_url="https://api.deepseek.com/v1", model="deepseek-chat",
                                          api_key_env=env, context_window=128000, max_output_tokens=8192)),
            "groq": ("groq", dict(provider="openai", base_url="https://api.groq.com/openai/v1", model="llama-3.3-70b-versatile",
                                  api_key_env=env, context_window=128000, max_output_tokens=8192)),
            "mistral": ("mistral", dict(provider="openai", base_url="https://api.mistral.ai/v1", model="mistral-large-latest",
                                        api_key_env=env, context_window=128000, max_output_tokens=8192))}
    return base.get(provider)


def plan(findings: list, cfg) -> list[Action]:
    """What setup would do with the findings; nothing is written here."""
    actions: list[Action] = []
    stored = auth.keys()
    profiles = cfg.models if cfg else {}
    envs_in_use = {mc.api_key_env for mc in profiles.values() if getattr(mc, "api_key_env", "")}
    seen_env = set()
    for f in findings:
        if f.kind != "key" or f.name in seen_env:
            continue
        seen_env.add(f.name)
        env, value = f.name, f.value
        if stored.get(env) == value:
            continue                                                  # already ours
        if os.environ.get(env) == value and "the environment" in f.source and "line" not in f.source:
            note = "already set by your shell — stored too, so a service or a new terminal has it"
        else:
            note = f"from {f.source}"
        actions.append(Action("key", f"store {env} = {f.masked}   ({note})",
                              lambda env=env, value=value: f"stored {env} in {auth.save_key(env, value)}"))
        provider = discover_mod.KEY_ENVS.get(env, "")
        if env not in envs_in_use and provider not in ("gitlab", "ollama"):
            preset = _preset_profile(provider, env)
            if preset and preset[0] not in profiles:
                name, fields = preset
                actions.append(Action("profile", f"add profile '{name}' ({fields['model']}) using {env}",
                                      lambda name=name, fields=fields: _add_profile(name, fields), data={"name": name}))
    existing_mcp = _home_mcp().get("mcpServers") or {}
    for f in findings:
        if f.kind == "mcp" and f.name not in existing_mcp:
            try:
                entry = json.loads(f.value)
            except ValueError:
                continue
            actions.append(Action("mcp", f"add MCP server '{f.name}' from {f.agent}",
                                  lambda name=f.name, entry=entry: _add_mcp(name, entry)))
    for f in findings:
        if f.kind == "endpoint" and f.data.get("base_url") and f.data.get("models"):
            base, model = f.data["base_url"], f.data["models"][0]
            if any(getattr(mc, "base_url", "") == base for mc in profiles.values()):
                continue
            name = _slug(f.name.split(":")[0]) or "server"
            if name in profiles:
                name = f"{name}-{len(profiles)}"
            fields = dict(provider="openai", base_url=base, model=model, context_window=65536, max_output_tokens=8192)
            actions.append(Action("endpoint", f"add profile '{name}' for {base} ({model}, seen in {f.agent})",
                                  lambda name=name, fields=fields: _add_profile(name, fields), default=False, data={"name": name}))
    return actions


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() or c == "-" else "-" for c in text.strip().lower()).strip("-")[:24]


def _home_mcp() -> dict:
    try:
        return json.loads((home_config_dir() / "mcp.json").read_text())
    except (OSError, ValueError):
        return {}


def _add_mcp(name: str, entry: dict) -> str:
    path = home_config_dir() / "mcp.json"
    data = _home_mcp()
    data.setdefault("mcpServers", {})[name] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return f"added MCP server '{name}' to {path}"


def _add_profile(name: str, fields: dict) -> str:
    from . import settings_edit
    return settings_edit.add_profile(name, fields)


# ---------------------------------------------------------------- the default profile

def _local_ollama(mc) -> bool:
    host = (getattr(mc, "base_url", "") or "").lower()
    return any(h in host for h in ("localhost", "127.0.0.1")) and ":11434" in host


def pick_default(cfg, findings: list, probe: Callable | None = None) -> tuple[str, str]:
    """(profile name, why) — the first profile that answers now, or ('', why nothing does)."""
    from .doctor import probe_model
    probe = probe or probe_model
    profiles = cfg.models
    ollama = next((f for f in findings if f.kind == "ollama" and f.data.get("reachable")), None)
    tried: list[str] = []

    def works(name: str, why: str) -> tuple[str, str] | None:
        mc = profiles[name]
        key = os.environ.get(mc.api_key_env, "") if getattr(mc, "api_key_env", "") else ""
        verdict = probe(mc, key)
        if verdict in ("ok", ""):
            return name, why
        tried.append(f"{name}: {verdict}")
        return None
    if ollama:
        for pool, why in ((ollama.data.get("loaded") or [], "loaded on your Ollama right now"),
                          (ollama.data.get("local") or [], "pulled on your Ollama")):
            for name, mc in profiles.items():
                if _local_ollama(mc) and mc.model in pool and (hit := works(name, why)):
                    return hit
        if ollama.data.get("local") and not any(_local_ollama(mc) and mc.model in ollama.data["local"] for mc in profiles.values()):
            model = ollama.data["local"][0]
            name = _slug(model.split(":")[0]) or "local"
            if name not in profiles:
                _add_profile(name, dict(provider="openai", base_url=f"{ollama.data['base_url']}/v1", model=model,
                                        context_window=32768, max_output_tokens=8192))
                cfg = load_config()
                profiles = cfg.models
                if hit := works(name, f"'{model}' is pulled on your Ollama (profile added)"):
                    return hit
        for name, mc in profiles.items():
            if _local_ollama(mc) and (mc.model.endswith(":cloud") or mc.model.endswith("-cloud")) and (hit := works(name, "Ollama Cloud through your signed-in Ollama")):
                return hit
    keyed = [(name, mc) for name, mc in profiles.items()
             if getattr(mc, "api_key_env", "") and os.environ.get(mc.api_key_env) and not _local_ollama(mc)]
    keyed.sort(key=lambda nm: next((i for i, w in enumerate(CHEAP_FIRST) if w in nm[1].model.lower()), len(CHEAP_FIRST)))
    for name, mc in keyed:
        if hit := works(name, f"your {mc.api_key_env} works"):
            return hit
    why = "no model can answer yet" + (f" (tried {'; '.join(tried[:4])})" if tried else "")
    return "", why


def missing_advice(findings: list, cfg) -> list[str]:
    """One ADVICE line per provider that has nothing configured — the same wording the doctor uses."""
    have = {discover_mod.KEY_ENVS.get(f.name, "") for f in findings if f.kind == "key"} | set(
        discover_mod.KEY_ENVS.get(e, "") for e in auth.keys())
    ollama = next((f for f in findings if f.kind == "ollama"), None)
    lines = []
    if not ollama:
        lines.append("Ollama (free, local models; Ollama Cloud for big ones): https://ollama.com/download then `ollama pull qwen3.6:35b-a3b-coding`")
    elif not ollama.data.get("reachable"):
        lines.append("Ollama is installed but not running: `ollama serve` (or open the Ollama app)")
    if "anthropic" not in have:
        lines.append("Claude: https://console.anthropic.com/settings/keys → `hexiel auth set anthropic`")
    if "openai" not in have:
        lines.append("OpenAI: https://platform.openai.com/api-keys → `hexiel auth set openai`")
    if "gemini" not in have:
        lines.append("Gemini: https://aistudio.google.com/apikey → `hexiel auth set gemini`")
    if "ollama" not in have:
        lines.append("Ollama Cloud key (budget tracking, direct cloud access): https://ollama.com/settings/keys → `hexiel auth set OLLAMA_API_KEY`")
    return lines


# ---------------------------------------------------------------- the wizard

def run(cwd: str, ask=input, say=print, interactive: bool | None = None, network: bool = True) -> dict:
    """The whole flow. Returns what happened (for /import and tests): {"found": n, "applied": [...], "default": name}."""
    interactive = sys.stdin.isatty() if interactive is None else interactive
    say("Welcome to hexiel — setup (Enter keeps the default on every question)\n")
    fresh = not CONFIG_FILE.exists()
    cfg = load_config()                       # writes the starter config when there is none
    if fresh:
        say(f"  wrote {CONFIG_FILE}")
    auth.keys_into_env()
    findings = discover_mod.discover(cwd, network=network)
    say("Found on this machine:")
    for line in discover_mod.summary(findings):
        say("  " + line)
    actions = plan(findings, cfg)
    applied: list[str] = []
    if actions:
        say("\nhexiel can set itself up from that:")
        for a in actions:
            yes = a.default
            if interactive:
                answer = ask(f"  {a.label}  [{'Y/n' if a.default else 'y/N'}] ").strip().lower()
                yes = (answer.startswith("y")) if answer else a.default
            else:
                say(f"  · {a.label}" + ("" if yes else "  (skipped: not a default)"))
            if yes:
                try:
                    applied.append(a.apply())
                except Exception as exc:  # noqa: BLE001 - one failure must not stop the rest
                    applied.append(f"{a.label}: failed — {exc}")
        for line in applied:
            say("  ✓ " + line)
        auth.keys_into_env()
        cfg = load_config()
    name, why = pick_default(cfg, findings) if network else ("", "offline")
    cfg = load_config()                       # pick_default may have added a profile
    if name:
        if name != cfg.default_model:
            from . import settings_edit
            settings_edit._write(None, "default", name)
        say(f"\n✓ default model: '{name}' ({cfg.models[name].model}) — {why}")
    else:
        say(f"\n✗ {why}")
    advice = missing_advice(findings, cfg)
    if advice:
        say("\nNot set up yet (optional — add any time):")
        for line in advice:
            say("  - " + line)
    _remote_part(ask, say, interactive)
    say("\nDone. Start a session with `hexiel` (TUI), `hexiel --serve` (browser), or `hexiel -p \"…\"`.")
    return {"found": len(findings), "applied": applied, "default": name, "advice": advice}


def _remote_part(ask, say, interactive: bool) -> None:
    from . import remote
    from .gitlab import load_token
    if not load_token():
        say("\n  GitLab token: none — /ship and /heal push branches but will not open merge requests "
            "(add later with /setup-gitlab <token>).")
    if not remote.load_config().get("device_token"):
        say("  Hexiel Remote (paid): drive this machine from a browser or phone — `hexiel remote login` when you want it.")
        return
    enable = "n"
    if interactive:
        enable = ask("\nInstall the Hexiel Remote background service (starts at login)? [Y/n] ").strip().lower() or "y"
    if enable.startswith("y"):
        try:
            say("  " + remote.install_service())
        except (RuntimeError, OSError) as exc:
            say(f"  ! could not install the background service: {exc}")
    else:
        say("  skipped Hexiel Remote background service. Run `hexiel remote install` anytime.")


def import_report(cwd: str) -> str:
    """/import: discovery with the defaults, as text for a chat note."""
    lines: list[str] = []
    run(cwd, ask=lambda _p: "", say=lines.append, interactive=False)
    return "\n".join(lines)
