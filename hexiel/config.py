"""Configuration loading and defaults for hexiel."""

import dataclasses
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
auto_approve = false        # false = ask first | "except-commits" = auto-approve all but git/svn commits | true = everything
compact_threshold = 0.82    # auto-compact when the prompt reaches this share of the context window
request_timeout = 900       # seconds per provider request
search_backend = "duckduckgo"   # "duckduckgo" (no key) or "searxng"
searxng_url = ""            # e.g. http://localhost:8888 when search_backend = "searxng"

# compact_profile = "anthropic-sonnet"   # who writes compaction summaries: a fast cloud model takes seconds, a slow local one many minutes

# Per-tool models: a small, fast model reads big outputs for the main one (digest_min_chars is the size that triggers it).
# The main model can ask for the raw text with full=true. Tools that write (write, edit, bash) always use the main model;
# for planning set planner_profile below.
# [tool_models]
# read = "small-local"
# grep = "small-local"
# web_fetch = "small-local"
# search = "strong-local"       # alias for web_search
# digest_min_chars = 3000       # (in [settings]) smaller outputs are never summarised
# digest_timeout = 90           # (in [settings]) seconds a tool model gets; slower than that and the raw output is used

# Big tasks: a strong model plans, a cheaper (e.g. local) model does the work. See /delegate.
# cloud_fallback_profile = "glm-flash"   # cloud tasks only (plans, advice, compaction summaries): used when the planner/summary model fails or is limited
# planner_profile = "kimi"      # model that writes the plan ("" = fallback_profile, else the current model)
# workers = "auto"             # steps run at once when independent: "auto" = every core but one, or a number (1 = one at a time)
# worker_fallback_profile = ""  # used for a step while the worker is busy (e.g. a slow local model serving another session)
# executor_profile = ""         # model that does each task ("" = the current model)
# auto_delegate = "ask"         # "off" | "ask" (offer to delegate big requests) | "auto"
# delegate_approval = true      # show the plan and wait for a yes before running it
# delegate_retries = 2          # fix attempts per task before escalating it to the planner model
# escalate_mode = "auto"        # when the working model is struggling the planner model takes over for a few steps: "auto" | "off"
# consult_mode = "auto"         # ask the planner model for advice when stuck: "auto" | "tool" (only if the model asks) | "off"
# delegate_review = "auto"      # the planner reviews the finished diff and fixes problems: "auto" | "always" | "off"
# error_reports = "account"     # send hexiel's own errors (scrubbed of paths and secrets) for tracking: "account" (signed in to Hexiel Remote) | "all" (anyone, to the public queue at hexiel.tech/support) | "off"

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
    auto_approve: bool = False        # run edits and commands without asking (see guard_commits)
    guard_commits: bool = False       # with auto_approve: still ask before anything that commits/pushes (git, svn)
    compact_threshold: float = 0.82
    compact_profile: str = ""         # model that writes compaction summaries ("" = the current model); a fast cloud model avoids long waits
    request_timeout: int = 900
    search_backend: str = "duckduckgo"
    searxng_url: str = ""
    auto_resume: bool = True          # wait out rate limits and continue automatically
    auto_resume_max_wait: int = 21600 # longest wait (s) before giving up / falling back
    fallback_profile: str = ""        # switch here when a limit won't reset soon (e.g. a local model)
    notify: bool = True               # desktop notification when approval is needed / long turns finish
    auto_commit: bool = False         # git-commit the files hexiel changed after each finished turn
    subagent_profile: str = ""        # model profile for `task` subagents ("" = the current model)
    toolsets: object = "auto"         # optional tool groups: "auto" | "all" | ["data", …]
    planner_profile: str = ""         # /delegate: model that writes the plan ("" = fallback_profile, else the current model)
    executor_profile: str = ""        # /delegate: model that does each task ("" = the current model)
    workers: object = "auto"          # /delegate: steps run at once when independent — "auto" = every core but one, or a number
    worker_fallback_profile: str = "" # /delegate: used for a step while the worker model is busy ("" = always wait for the worker)
    router_profile: str = ""          # a small cloud model that sorts each typed message (chat/small/work/big) — "" = no router
    overflow_profile: str = ""        # while the main model is busy, chat-sized requests run here ("" = queue as usual)
    overflow_daily_tokens: int = 0    # daily cap on the router model's tokens (routing, chat answers, overflow); 0 = no cap
    auto_delegate: str = "off"        # size up each request: "off" | "ask" (offer to delegate big ones) | "auto"
    delegate_approval: bool = True    # show the plan and wait for a yes before running it
    delegate_retries: int = 2         # fix attempts per task before escalating it to the planner model
    escalate_mode: str = "auto"       # when the working model is struggling, let the planner model take over for a few steps: "auto" | "off"
    consult_mode: str = "auto"        # ask the planner model for advice: "auto" (the model may ask, and hexiel asks when it is stuck) | "tool" (only when the model asks) | "off"
    error_reports: str = "account"    # send hexiel's own errors to remote.hexiel.tech for tracking: "account" (when signed in to Hexiel Remote) | "all" (also anonymously, to the public queue) | "off"
    cloud_fallback_profile: str = ""  # cloud tasks only (plans, advice, compaction summaries): used when the cloud model fails or is limited
    tool_models: dict = field(default_factory=dict)   # [tool_models]: tool -> profile that condenses its big output (see digest.py)
    digest_min_chars: int = 3000      # tool output shorter than this is never condensed by a tool model
    digest_timeout: int = 90          # seconds a tool model gets to condense one output before hexiel uses the raw text
    delegate_review: str = "auto"     # after a delegated run the planner model reviews the diff: "auto" (when it is a different model than the worker) | "always" | "off"


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
    allowed = sorted(f.name for f in dataclasses.fields(ModelConfig) if f.name != "name")
    for name, body in raw.get("models", {}).items():
        if not isinstance(body, dict):
            raise SystemExit(f"bad config {CONFIG_FILE}: [models.{name}] must be a table, e.g. [models.{name}] on its own line")
        unknown = sorted(set(body) - set(allowed))
        if unknown:
            raise SystemExit(f"bad config {CONFIG_FILE}: [models.{name}] has unknown setting(s): {', '.join(unknown)} "
                             f"(allowed: {', '.join(allowed)})")
        missing = [k for k in ("provider", "model") if k not in body]
        if missing:
            raise SystemExit(f"bad config {CONFIG_FILE}: [models.{name}] needs {' and '.join(missing)}")
        models[name] = ModelConfig(name=name, **body)

    settings_raw = raw.get("settings", {})
    try:
        settings = _settings_from(settings_raw)
    except (ValueError, TypeError) as exc:
        raise SystemExit(f"bad config {CONFIG_FILE}: a [settings] value has the wrong type or format ({exc})")

    default_model = model_override or raw.get("default", next(iter(models), "default"))
    if default_model not in models:
        have = ", ".join(models) or "none yet"
        if model_override:    # callers that pass ad-hoc profile names catch this and fall back to the defaults
            raise ValueError(f"--model {default_model!r} is not one of your [models.*] profiles (you have: {have})")
        raise SystemExit(f"bad config {CONFIG_FILE}: default = {default_model!r} is not one of your [models.*] profiles "
                         f"(you have: {have}). Add a [models.NAME] table — see https://hexiel.tech/config/")
    tm = raw.get("tool_models", {})
    if isinstance(tm, dict):
        settings.tool_models = {str(k): (v if isinstance(v, (dict, list)) else str(v)) for k, v in tm.items()}
    return Config(default_model=default_model, models=models, settings=settings,
                  mcp=raw.get("mcp", {}), permissions=raw.get("permissions", {}))


APPROVE_MODES = ("off", "except-commits", "all")


def parse_auto_approve(value) -> str:
    """auto_approve = false | "except-commits" | true  ->  "off" | "except-commits" | "all"."""
    if value is None or value is False:
        return "off"
    if value is True:
        return "all"
    if isinstance(value, str):
        v = value.strip().lower().replace("_", "-").replace(" ", "-")
        if v in ("off", "false", "no", "0", "ask"):
            return "off"
        if v in ("except-commits", "no-commits", "except-commit", "safe"):
            return "except-commits"
        if v in ("all", "true", "yes", "on", "1", "everything"):
            return "all"
    raise ValueError('auto_approve must be false, true or "except-commits"')


def approve_mode(auto: bool, guard: bool) -> str:
    return "off" if not auto else ("except-commits" if guard else "all")


def _settings_from(settings_raw: dict) -> "Settings":
    mode = parse_auto_approve(settings_raw.get("auto_approve", False))
    return Settings(
        auto_approve=mode != "off",
        guard_commits=mode == "except-commits",
        compact_threshold=float(settings_raw.get("compact_threshold", 0.82)),
        compact_profile=str(settings_raw.get("compact_profile", "")),
        cloud_fallback_profile=str(settings_raw.get("cloud_fallback_profile", "")),
        digest_min_chars=max(500, int(settings_raw.get("digest_min_chars", 3000))),
        digest_timeout=max(5, int(settings_raw.get("digest_timeout", 90))),
        request_timeout=int(settings_raw.get("request_timeout", 900)),
        search_backend=str(settings_raw.get("search_backend", "duckduckgo")),
        searxng_url=str(settings_raw.get("searxng_url", "")),
        auto_resume=bool(settings_raw.get("auto_resume", True)),
        auto_resume_max_wait=int(settings_raw.get("auto_resume_max_wait", 21600)),
        fallback_profile=str(settings_raw.get("fallback_profile", "")),
        notify=bool(settings_raw.get("notify", True)),
        auto_commit=bool(settings_raw.get("auto_commit", False)),
        subagent_profile=str(settings_raw.get("subagent_profile", "")),
        toolsets=settings_raw.get("toolsets", "auto"),
        planner_profile=str(settings_raw.get("planner_profile", "")),
        executor_profile=str(settings_raw.get("executor_profile", "")),
        workers=(lambda v: max(1, int(v)) if str(v).isdigit() else "auto")(settings_raw.get("workers", "auto")),
        worker_fallback_profile=str(settings_raw.get("worker_fallback_profile", "")),
        router_profile=str(settings_raw.get("router_profile", "")),
        overflow_profile=str(settings_raw.get("overflow_profile", "")),
        overflow_daily_tokens=max(0, int(settings_raw.get("overflow_daily_tokens", 0))),
        auto_delegate=(str(settings_raw.get("auto_delegate", "off")).lower()
                       if str(settings_raw.get("auto_delegate", "off")).lower() in ("off", "ask", "auto") else "off"),
        delegate_approval=bool(settings_raw.get("delegate_approval", True)),
        delegate_retries=max(0, int(settings_raw.get("delegate_retries", 2))),
        escalate_mode="off" if str(settings_raw.get("escalate_mode", "auto")).lower() == "off" else "auto",
        consult_mode=(str(settings_raw.get("consult_mode", "auto")).lower()
                      if str(settings_raw.get("consult_mode", "auto")).lower() in ("off", "tool", "auto") else "auto"),
        error_reports=(str(settings_raw.get("error_reports", "account")).lower()
                       if str(settings_raw.get("error_reports", "account")).lower() in ("account", "all", "off") else "account"),
        delegate_review=(str(settings_raw.get("delegate_review", "auto")).lower()
                         if str(settings_raw.get("delegate_review", "auto")).lower() in ("off", "auto", "always") else "auto"),
    )
