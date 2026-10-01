"""Configuration loading and defaults for hexiel."""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def home_config_dir() -> Path:
    return Path(os.environ.get("HEXIEL_HOME", Path.home() / ".config" / "hexiel"))


def home_data_dir() -> Path:
    return Path(os.environ.get("HEXIEL_DATA", Path.home() / ".local" / "share" / "hexiel"))


def memory_dir() -> Path:
    return home_data_dir() / "memory"


def skills_dirs() -> list[Path]:
    dirs = [home_data_dir() / "skills"]
    # compat with existing Claude Code skills so nothing has to be copied over
    dirs.append(Path.home() / ".claude" / "skills")
    dirs.append(Path.cwd() / ".hexiel" / "skills")
    dirs.append(Path.cwd() / ".claude" / "skills")
    return dirs


CONFIG_FILE = home_config_dir() / "config.toml"
OLLAMA_HOST = "http://localhost:11434"


def list_ollama_models(base_url: str = "") -> list[str]:
    """Live model inventory from an Ollama server (local or cloud-backed)."""
    import httpx
    root = (base_url or OLLAMA_HOST).removesuffix("/v1").removesuffix("/")
    try:
        resp = httpx.get(f"{root}/api/tags", timeout=10)
        return sorted(m["name"] for m in resp.json().get("models", []))
    except (httpx.HTTPError, ValueError):
        return []

DEFAULT_CONFIG = '''\
# hexiel configuration. Profiles live under [models.*]; pick one with /model NAME
# or `hexiel --model NAME`. "default" selects the boot model.

default = "glm-flash"

[settings]
auto_approve = false        # when true, write tools and bash run without approval
compact_threshold = 0.82    # auto-compact when the prompt reaches this share of the context window
request_timeout = 900       # seconds per provider request
search_backend = "duckduckgo"   # "duckduckgo" (no key) or "searxng"
searxng_url = ""            # e.g. http://localhost:8888 when search_backend = "searxng"

[models.glm-flash]
provider = "openai"         # "openai" = any OpenAI-compatible /chat/completions endpoint
base_url = "http://localhost:11434/v1"
model = "glm-5.3-flash:cloud"
context_window = 200000     # tokens before auto-compaction kicks in
max_output_tokens = 16384

[models.kimi]
provider = "openai"
base_url = "http://localhost:11434/v1"
model = "kimi-k2.7-code:cloud"
context_window = 262144
max_output_tokens = 16384

[models.anthropic]
provider = "anthropic"
model = "claude-opus-5-5"
api_key_env = "ANTHROPIC_API_KEY"
context_window = 200000
max_output_tokens = 16384

[models.anthropic-sonnet]
provider = "anthropic"
model = "claude-sonnet-5-5"
api_key_env = "ANTHROPIC_API_KEY"
context_window = 200000
max_output_tokens = 16384

# ---- more big-model presets (fill in the key env var / base_url you have) ----

[models.openai]
provider = "openai"
base_url = "https://api.openai.com/v1"
model = "gpt-5.2"
api_key_env = "OPENAI_API_KEY"
context_window = 400000
max_output_tokens = 16384

[models.gemini]
provider = "openai"         # Gemini speaks the OpenAI-compatible protocol
base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
model = "gemini-2.5-pro"
api_key_env = "GEMINI_API_KEY"
context_window = 1000000
max_output_tokens = 16384

[models.xai]
provider = "openai"
base_url = "https://api.x.ai/v1"
model = "grok-4"
api_key_env = "XAI_API_KEY"
context_window = 256000
max_output_tokens = 16384

[models.openrouter]
provider = "openai"         # hundreds of models behind one key
base_url = "https://openrouter.ai/api/v1"
model = "anthropic/claude-opus-4.5"
api_key_env = "OPENROUTER_API_KEY"
context_window = 200000
max_output_tokens = 16384

[models.deepseek]
provider = "openai"
base_url = "https://api.deepseek.com/v1"
model = "deepseek-chat"
api_key_env = "DEEPSEEK_API_KEY"
context_window = 128000
max_output_tokens = 16384
'''


@dataclass
class ModelConfig:
    name: str
    provider: str
    model: str
    base_url: str = ""
    api_key_env: str = ""
    context_window: int = 200_000
    max_output_tokens: int = 16_384
    extra_body: dict = field(default_factory=dict)
    echo_reasoning: bool = False     # send reasoning back as reasoning_content (DeepSeek, Together, Kimi)
    max_tokens_param: str = ""       # "" = auto (max_completion_tokens for OpenAI/Azure, else max_tokens)


@dataclass
class Settings:
    auto_approve: bool = False
    compact_threshold: float = 0.82
    request_timeout: int = 900
    search_backend: str = "duckduckgo"
    searxng_url: str = ""
    auto_resume: bool = True          # wait out rate limits and continue automatically
    auto_resume_max_wait: int = 21600 # longest wait (s) before giving up / falling back
    fallback_profile: str = ""        # switch here when a limit won't reset soon (e.g. a local model)
    notify: bool = True               # desktop notification when approval is needed / long turns finish


@dataclass
class Config:
    default_model: str
    models: dict[str, ModelConfig]
    settings: Settings
    mcp: dict = field(default_factory=dict)       # raw [mcp] table (servers)
    permissions: dict = field(default_factory=dict)   # raw [permissions] allow/ask/deny

    def model(self, name: str | None = None) -> ModelConfig:
        key = name or self.default_model
        if key not in self.models:
            raise KeyError(f"unknown model profile '{key}' (have: {', '.join(self.models)})")
        return self.models[key]


def load_config(model_override: str | None = None) -> Config:
    if not CONFIG_FILE.exists():
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(DEFAULT_CONFIG)
        except OSError:
            pass

    raw: dict = {}
    if CONFIG_FILE.exists():
        try:
            raw = tomllib.loads(CONFIG_FILE.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise SystemExit(f"bad config {CONFIG_FILE}: {exc}")

    models: dict[str, ModelConfig] = {}
    for name, body in raw.get("models", {}).items():
        models[name] = ModelConfig(name=name, **body)

    settings_raw = raw.get("settings", {})
    settings = Settings(
        auto_approve=bool(settings_raw.get("auto_approve", False)),
        compact_threshold=float(settings_raw.get("compact_threshold", 0.82)),
        request_timeout=int(settings_raw.get("request_timeout", 900)),
        search_backend=str(settings_raw.get("search_backend", "duckduckgo")),
        searxng_url=str(settings_raw.get("searxng_url", "")),
        auto_resume=bool(settings_raw.get("auto_resume", True)),
        auto_resume_max_wait=int(settings_raw.get("auto_resume_max_wait", 21600)),
        fallback_profile=str(settings_raw.get("fallback_profile", "")),
        notify=bool(settings_raw.get("notify", True)),
    )

    default_model = model_override or raw.get("default", next(iter(models), "default"))
    if default_model not in models:
        models.setdefault(default_model, ModelConfig(name=default_model, provider="openai"))
    return Config(default_model=default_model, models=models, settings=settings,
                  mcp=raw.get("mcp", {}), permissions=raw.get("permissions", {}))