"""What this machine already has, found read-only in the usual places on Linux, macOS and Windows: the API
keys, MCP servers, skills and rules of the other coding agents (Claude Code, Codex, Gemini CLI, OpenCode,
Goose, Aider, Continue, Cursor), keys exported in shell profiles and .env files, Windows user variables,
keychain entries other agents stored, and the local Ollama (running? which models? signed in to the cloud?).

First-run setup (setup.py) turns the findings into hexiel's config after a review; nothing here writes.
Rules that matter:
  * only API keys are taken — a subscription login (Claude Code's claude.ai login, Codex's ChatGPT login,
    Gemini's Google login) is reported by name and refused: hexiel cannot use it, and the alternative is said
  * every finding carries its provenance ("ANTHROPIC_API_KEY in ~/.zshrc line 42") and is masked when shown
  * unknown variables are never taken: a .env with a database password stays where it is (KEY_ENVS lists
    the names hexiel understands)
"""

from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

KEY_ENVS = {
    "ANTHROPIC_API_KEY": "anthropic", "OPENAI_API_KEY": "openai", "GEMINI_API_KEY": "gemini", "GOOGLE_API_KEY": "gemini",  # pragma: allowlist secret
    "OPENROUTER_API_KEY": "openrouter", "XAI_API_KEY": "xai", "DEEPSEEK_API_KEY": "deepseek", "OLLAMA_API_KEY": "ollama",  # pragma: allowlist secret
    "GROQ_API_KEY": "groq", "MISTRAL_API_KEY": "mistral", "TOGETHER_API_KEY": "together", "FIREWORKS_API_KEY": "fireworks",  # pragma: allowlist secret
    "PERPLEXITY_API_KEY": "perplexity", "MOONSHOT_API_KEY": "moonshot", "ZAI_API_KEY": "zai",  # pragma: allowlist secret
    "AZURE_OPENAI_API_KEY": "azure", "HEXIEL_GITLAB_TOKEN": "gitlab", "GITLAB_TOKEN": "gitlab",  # pragma: allowlist secret
}
PROVIDER_ENV = {v: k for k, v in reversed(list(KEY_ENVS.items()))}      # provider -> its usual variable
_SHELL_LINE = re.compile(r"""^\s*(?:export\s+|set\s+-g?x\s+|setenv\s+)?([A-Z][A-Z0-9_]*)\s*[= ]\s*["']?([^"'\s#]+)""")
_SECRET_LIKE = re.compile(r"(key|token|secret|password|credential)", re.I)


@dataclass
class Finding:
    kind: str                  # key | login | mcp | skills | rules | ollama | endpoint
    name: str                  # key: variable name; mcp: server name; login/skills/rules/endpoint: a label
    source: str                # where it was found (provenance)
    agent: str = ""            # the product it belongs to
    value: str = ""            # the secret, never printed (see masked); mcp: the entry as JSON
    note: str = ""             # why it is or is not usable
    data: dict = field(default_factory=dict)

    @property
    def masked(self) -> str:
        v = self.value or ""
        return f"{v[:7]}…{v[-3:]}" if len(v) > 12 else ("set" if v else "")

    def line(self) -> str:
        """One human line, secrets masked."""
        if self.kind == "key":
            return f"{self.name} = {self.masked}   ({self.source})"
        if self.kind == "login":
            return f"{self.name}   ({self.source}) — {self.note}"
        if self.kind == "mcp":
            return f"MCP server '{self.name}'   ({self.source})"
        return f"{self.name}   ({self.source})" + (f" — {self.note}" if self.note else "")


# ---------------------------------------------------------------- where things live

def _home() -> Path:
    return Path(os.environ.get("HEXIEL_DISCOVER_HOME") or Path.home())


def _windows() -> bool:
    return os.name == "nt"


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or (_home() / "AppData" / "Roaming"))


def _tilde(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(_home())).replace("\\", "/")
    except ValueError:
        return str(path)


def _json(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        text = re.sub(r"^\s*//.*$", "", text, flags=re.M)            # .jsonc comments
        return json.loads(text) if text.strip() else {}
    except (OSError, ValueError):
        return {}


def _toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return {}


def _key(name: str, value, source: str, agent: str) -> Finding | None:
    if name in KEY_ENVS and isinstance(value, str) and value.strip() and not value.startswith("${"):
        return Finding("key", name, source, agent, value.strip())
    return None


# ---------------------------------------------------------------- one scanner per source

def scan_environment() -> list[Finding]:
    out = [f for name in KEY_ENVS if (f := _key(name, os.environ.get(name, ""), "the environment", "shell"))]
    if _windows():
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as hk:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(hk, i)
                    except OSError:
                        break
                    i += 1
                    if f := _key(name, value, "Windows user environment variables", "Windows"):
                        out.append(f)
        except (ImportError, OSError):
            pass
    return out


def scan_shell_files() -> list[Finding]:
    home = _home()
    files = [home / n for n in (".bashrc", ".bash_profile", ".zshrc", ".zshenv", ".zprofile", ".profile", ".env")]
    files += [home / ".config" / "fish" / "config.fish"]
    files += sorted((home / ".config" / "fish" / "conf.d").glob("*.fish")) if (home / ".config" / "fish" / "conf.d").is_dir() else []
    files += sorted((home / ".config" / "environment.d").glob("*.conf")) if (home / ".config" / "environment.d").is_dir() else []
    out = []
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for n, line in enumerate(lines, 1):
            m = _SHELL_LINE.match(line)
            if m and (f := _key(m.group(1), m.group(2), f"{_tilde(path)} line {n}", "shell")):
                out.append(f)
    return out


def scan_claude_code() -> list[Finding]:
    home = _home()
    out = []
    cfg = _json(home / ".claude.json")
    for name, entry in (cfg.get("mcpServers") or {}).items():
        if isinstance(entry, dict):
            out.append(Finding("mcp", name, _tilde(home / ".claude.json"), "Claude Code", json.dumps(entry)))
    settings = _json(home / ".claude" / "settings.json")
    for name, value in (settings.get("env") or {}).items():
        if f := _key(name, value, _tilde(home / ".claude" / "settings.json") + " [env]", "Claude Code"):
            out.append(f)
    creds = _json(home / ".claude" / ".credentials.json")
    if isinstance(creds.get("claudeAiOauth"), dict) or cfg.get("oauthAccount"):
        out.append(Finding("login", "Claude Code — claude.ai subscription login", _tilde(home / ".claude"), "Claude Code",
                           note="a subscription login, not an API key: hexiel cannot use it. Use an Anthropic API key "
                                "(https://console.anthropic.com/settings/keys) or a local/Ollama Cloud model instead"))
    skills = home / ".claude" / "skills"
    if skills.is_dir():
        n = sum(1 for p in skills.iterdir() if p.is_dir())
        if n:
            out.append(Finding("skills", f"{n} Claude Code skill(s)", _tilde(skills), "Claude Code",
                               note="read as-is by hexiel — nothing to import", data={"count": n}))
    for rel in ("CLAUDE.md",):
        if (home / ".claude" / rel).is_file():
            out.append(Finding("rules", rel, _tilde(home / ".claude" / rel), "Claude Code", note="read as-is by hexiel"))
    return out


def scan_codex() -> list[Finding]:
    home = _home()
    out = []
    auth = _json(home / ".codex" / "auth.json")
    if f := _key("OPENAI_API_KEY", auth.get("OPENAI_API_KEY"), _tilde(home / ".codex" / "auth.json"), "Codex"):
        out.append(f)
    if isinstance(auth.get("tokens"), dict) and auth["tokens"]:
        out.append(Finding("login", "Codex — ChatGPT login", _tilde(home / ".codex" / "auth.json"), "Codex",
                           note="a ChatGPT subscription login, not an API key: hexiel cannot use it. Use an OpenAI API key "
                                "(https://platform.openai.com/api-keys) instead"))
    cfg = _toml(home / ".codex" / "config.toml")
    for name, entry in (cfg.get("mcp_servers") or {}).items():
        if isinstance(entry, dict):
            shaped = {k: v for k, v in entry.items() if k in ("command", "args", "env", "url")}
            out.append(Finding("mcp", name, _tilde(home / ".codex" / "config.toml"), "Codex", json.dumps(shaped)))
    if (home / ".codex" / "AGENTS.md").is_file():
        out.append(Finding("rules", "AGENTS.md", _tilde(home / ".codex" / "AGENTS.md"), "Codex", note="read as-is by hexiel"))
    return out


def scan_gemini() -> list[Finding]:
    home = _home()
    out = []
    env = home / ".gemini" / ".env"
    try:
        for n, line in enumerate(env.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            m = _SHELL_LINE.match(line)
            if m and (f := _key(m.group(1), m.group(2), f"{_tilde(env)} line {n}", "Gemini CLI")):
                out.append(f)
    except OSError:
        pass
    settings = _json(home / ".gemini" / "settings.json")
    for name, entry in (settings.get("mcpServers") or {}).items():
        if isinstance(entry, dict):
            out.append(Finding("mcp", name, _tilde(home / ".gemini" / "settings.json"), "Gemini CLI", json.dumps(entry)))
    if (home / ".gemini" / "oauth_creds.json").is_file():
        out.append(Finding("login", "Gemini CLI — Google login", _tilde(home / ".gemini" / "oauth_creds.json"), "Gemini CLI",
                           note="a Google account login, not an API key: hexiel cannot use it. Create a key at "
                                "https://aistudio.google.com/apikey (GEMINI_API_KEY) instead"))
    if (home / ".gemini" / "GEMINI.md").is_file():
        out.append(Finding("rules", "GEMINI.md", _tilde(home / ".gemini" / "GEMINI.md"), "Gemini CLI", note="read as-is by hexiel"))
    return out


def scan_opencode() -> list[Finding]:
    home = _home()
    out = []
    data_dir = (_appdata() if _windows() else Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")) / "opencode"
    auth = _json(data_dir / "auth.json")
    for provider, entry in auth.items():
        if not isinstance(entry, dict):
            continue
        env = PROVIDER_ENV.get(str(provider).lower())
        if entry.get("type") == "api" and env and (f := _key(env, entry.get("key"), _tilde(data_dir / "auth.json"), "OpenCode")):
            out.append(f)
        elif entry.get("type") in ("oauth", "wellknown"):
            out.append(Finding("login", f"OpenCode — {provider} login", _tilde(data_dir / "auth.json"), "OpenCode",
                               note="a subscription login, not an API key: hexiel cannot use it"))
    cfg_dir = (_appdata() if _windows() else Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")) / "opencode"
    cfg = {}
    for name in ("opencode.json", "opencode.jsonc", "config.json"):
        cfg = _json(cfg_dir / name)
        if cfg:
            break
    for name, entry in (cfg.get("mcp") or {}).items():
        if isinstance(entry, dict):
            shaped = {}
            if isinstance(entry.get("command"), list) and entry["command"]:
                shaped = {"command": entry["command"][0], "args": entry["command"][1:], "env": entry.get("environment") or {}}
            elif entry.get("url"):
                shaped = {"url": entry["url"]}
            if shaped:
                out.append(Finding("mcp", name, _tilde(cfg_dir / "opencode.json"), "OpenCode", json.dumps(shaped)))
    for pname, p in (cfg.get("provider") or {}).items():
        base = ((p or {}).get("options") or {}).get("baseURL") if isinstance(p, dict) else ""
        models = list(((p or {}).get("models") or {}).keys()) if isinstance(p, dict) else []
        if base and models:
            out.append(Finding("endpoint", f"{pname}: {base}", _tilde(cfg_dir / "opencode.json"), "OpenCode",
                               note=f"{len(models)} model(s): {', '.join(models[:4])}", data={"base_url": base, "models": models}))
    return out


def scan_goose() -> list[Finding]:
    home = _home()
    cfg = (_appdata() / "Block" / "goose" / "config" / "config.yaml") if _windows() else (home / ".config" / "goose" / "config.yaml")
    out = []
    try:
        text = cfg.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return out
    top = dict(re.findall(r"^([A-Z_]+):\s*\"?([^\"\n]+)\"?\s*$", text, flags=re.M))
    for name, value in top.items():
        if f := _key(name, value, _tilde(cfg), "Goose"):
            out.append(f)
    if top.get("GOOSE_PROVIDER"):
        out.append(Finding("endpoint", f"Goose uses {top['GOOSE_PROVIDER']} / {top.get('GOOSE_MODEL', '?')}", _tilde(cfg), "Goose",
                           data={"provider": top["GOOSE_PROVIDER"], "model": top.get("GOOSE_MODEL", "")}))
    return out


def scan_aider_continue() -> list[Finding]:
    home = _home()
    out = []
    aider = home / ".aider.conf.yml"
    try:
        for n, line in enumerate(aider.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            m = re.match(r"^\s*(openai|anthropic|gemini|openrouter|deepseek)-api-key:\s*[\"']?([^\"'\s#]+)", line)
            if m and (f := _key(PROVIDER_ENV[m.group(1)], m.group(2), f"{_tilde(aider)} line {n}", "Aider")):
                out.append(f)
    except OSError:
        pass
    cont = home / ".continue"
    for name in ("config.json",):
        cfg = _json(cont / name)
        for m in cfg.get("models") or []:
            if isinstance(m, dict) and m.get("apiKey") and PROVIDER_ENV.get(str(m.get("provider", "")).lower()):
                if f := _key(PROVIDER_ENV[str(m["provider"]).lower()], m["apiKey"], _tilde(cont / name), "Continue"):
                    out.append(f)
    yaml = cont / "config.yaml"
    try:
        text = yaml.read_text(encoding="utf-8", errors="ignore")
        for provider, key in re.findall(r"provider:\s*(\w+)[\s\S]*?apiKey:\s*[\"']?([^\"'\s#]+)", text):
            if PROVIDER_ENV.get(provider.lower()) and (f := _key(PROVIDER_ENV[provider.lower()], key, _tilde(yaml), "Continue")):
                out.append(f)
    except OSError:
        pass
    return out


def scan_cursor(cwd: str) -> list[Finding]:
    home = _home()
    out = []
    for path, agent in ((home / ".cursor" / "mcp.json", "Cursor"), (Path(cwd) / ".cursor" / "mcp.json", "Cursor (project)")):
        for name, entry in (_json(path).get("mcpServers") or {}).items():
            if isinstance(entry, dict):
                out.append(Finding("mcp", name, _tilde(path), agent, json.dumps(entry)))
    rules = Path(cwd) / ".cursor" / "rules"
    if rules.is_dir():
        out.append(Finding("rules", ".cursor/rules", str(rules), "Cursor", note="read as-is by hexiel"))
    return out


def scan_keyring() -> list[Finding]:
    """Keys other agents put in the OS keychain (macOS Keychain, Secret Service, Windows Credential Manager).
    Needs the optional `keyring` package; silently nothing without it."""
    try:
        import keyring
    except ImportError:
        return []
    out = []
    for service, label in (("goose", "Goose"), ("aider", "Aider"), ("opencode", "OpenCode")):
        for env in KEY_ENVS:
            try:
                value = keyring.get_password(service, env)
            except Exception:  # noqa: BLE001 - a locked or missing backend
                value = None
            if f := _key(env, value, f"{label} entry in the system keychain", label):
                out.append(f)
    return out


def scan_ollama() -> list[Finding]:
    """The local Ollama: reachable, which models, whether the cloud is signed in (one tiny request)."""
    from .config import OLLAMA_HOST, list_ollama_models
    out = []
    base = OLLAMA_HOST.rstrip("/")
    models = list_ollama_models(base)
    if not models and not Path(_home() / ".ollama").exists():
        return out
    if not models:
        out.append(Finding("ollama", "Ollama is installed but not running", _tilde(_home() / ".ollama"), "Ollama",
                           note="start it: ollama serve (or open the Ollama app)", data={"reachable": False}))
        return out
    local = [m for m in models if not m.endswith(":cloud")]
    cloud = [m for m in models if m.endswith(":cloud")]
    loaded = []
    try:
        import httpx
        loaded = [m.get("name") or m.get("model") for m in httpx.get(f"{base}/api/ps", timeout=5).json().get("models", [])]
    except Exception:  # noqa: BLE001
        pass
    out.append(Finding("ollama", f"Ollama running at {base}", base, "Ollama",
                       note=f"{len(local)} local model(s)" + (f", {len(cloud)} cloud" if cloud else "")
                            + (f"; loaded now: {', '.join(loaded)}" if loaded else ""),
                       data={"reachable": True, "base_url": base, "local": local, "cloud": cloud, "loaded": loaded}))
    return out


# ---------------------------------------------------------------- all of it

def discover(cwd: str = ".", network: bool = True) -> list[Finding]:
    """Everything found, deduplicated: the same key in two places is one finding with both sources."""
    found: list[Finding] = []
    for scanner in (scan_environment, scan_shell_files, scan_claude_code, scan_codex, scan_gemini, scan_opencode,
                    scan_goose, scan_aider_continue, lambda: scan_cursor(cwd), scan_keyring):
        try:
            found += scanner()
        except Exception:  # noqa: BLE001 - one broken file must not hide the others
            continue
    if network:
        try:
            found += scan_ollama()
        except Exception:  # noqa: BLE001
            pass
    out: list[Finding] = []
    seen: dict[tuple, Finding] = {}
    for f in found:
        ident = (f.kind, f.name, f.value if f.kind == "key" else "")
        if ident in seen:
            if f.source not in seen[ident].source:
                seen[ident].source += f"; also {f.source}"
            continue
        seen[ident] = f
        out.append(f)
    order = {"ollama": 0, "key": 1, "login": 2, "endpoint": 3, "mcp": 4, "skills": 5, "rules": 6}
    out.sort(key=lambda f: (order.get(f.kind, 9), f.name))
    return out


def summary(findings: list[Finding]) -> list[str]:
    """Human lines, grouped, secrets masked."""
    lines = []
    for kind, title in (("ollama", "Ollama"), ("key", "API keys"), ("login", "Logins hexiel cannot use"),
                        ("endpoint", "Model servers other agents use"), ("mcp", "MCP servers"),
                        ("skills", "Skills"), ("rules", "Instruction files")):
        rows = [f for f in findings if f.kind == kind]
        if rows:
            lines.append(f"{title}:")
            lines += [f"  · {f.line()}" for f in rows]
    return lines or ["nothing found — no other agent, key or Ollama on this machine"]


_ = sys  # keep the import for callers that monkeypatch sys.platform through this module
