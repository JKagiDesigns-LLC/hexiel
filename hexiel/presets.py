"""Ready-made model profiles for the major providers (checked 2026-10-01).

`hexiel --presets` lists them, `/model add <preset>` copies one into
config.toml, and docs/MODELS.md is generated from this table.

Cloud presets cap context_window at 200k even when the model accepts 1M: auto-
compaction triggers at 82% of the window, so a 1M window means requests of up
to ~800k tokens each — raise it deliberately for huge codebases."""

# group, name, label, fields, note
PRESETS = [
    # ---------------------------------------------------------------- local
    ("Local (free, private)", "ollama-qwen", "Ollama · Qwen3.6 35B-A3B coding (vision + tools, fast MoE)",
     dict(provider="openai", base_url="http://localhost:11434/v1", model="qwen3.6:35b-a3b-coding",
          context_window=65536, max_output_tokens=8192, extra_body={"reasoning_effort": "none"}),
     "Best all-round local model on modest hardware (~25 GB). Match context_window to the "
     "server's OLLAMA_CONTEXT_LENGTH. `ollama pull qwen3.6:35b-a3b-coding`."),
    ("Local (free, private)", "ollama-gpt-oss", "Ollama · gpt-oss 20B (tools, small)",
     dict(provider="openai", base_url="http://localhost:11434/v1", model="gpt-oss:20b",
          context_window=65536, max_output_tokens=8192),
     "Fits ~16 GB machines; text only. `ollama pull gpt-oss:20b`."),
    ("Local (free, private)", "lmstudio", "LM Studio (whatever model you have loaded)",
     dict(provider="openai", base_url="http://localhost:1234/v1", model="local-model",
          context_window=32768, max_output_tokens=8192),
     "Set `model` to the identifier LM Studio shows for the loaded model."),
    ("Local (free, private)", "vllm", "vLLM server",
     dict(provider="openai", base_url="http://localhost:8000/v1", model="your/served-model",
          context_window=65536, max_output_tokens=8192),
     "Start vLLM with tool calling enabled (--enable-auto-tool-choice + a tool parser)."),
    ("Local (free, private)", "llamacpp", "llama.cpp llama-server",
     dict(provider="openai", base_url="http://127.0.0.1:8080/v1", model="local",
          context_window=32768, max_output_tokens=8192),
     "Run llama-server with --jinja (needed for tool calls)."),
    # ---------------------------------------------------------------- frontier
    ("Anthropic", "sonnet", "Claude Sonnet 5.5",
     dict(provider="anthropic", model="claude-sonnet-5-5", api_key_env="ANTHROPIC_API_KEY",
          context_window=200000, max_output_tokens=32000),
     "Great default for hard coding work. Supports 1M context (raise context_window to use it)."),
    ("Anthropic", "opus", "Claude Opus 5.5",
     dict(provider="anthropic", model="claude-opus-5-5", api_key_env="ANTHROPIC_API_KEY",
          context_window=200000, max_output_tokens=32000),
     "Strongest Claude for complex, long tasks. Supports 1M context."),
    ("Anthropic", "haiku", "Claude Haiku 4.5",
     dict(provider="anthropic", model="claude-haiku-4-5", api_key_env="ANTHROPIC_API_KEY",
          context_window=200000, max_output_tokens=32000),
     "Fast and cheap."),
    ("OpenAI", "gpt", "OpenAI GPT-6 Astra",
     dict(provider="openai", base_url="https://api.openai.com/v1", model="gpt-6-astra",
          api_key_env="OPENAI_API_KEY", context_window=200000, max_output_tokens=32000),
     "Tool calling over Chat Completions. Note: gpt-6.1-sol has no tools on Chat Completions and "
     "gpt-5.3-codex is Responses-API-only — neither works as a hexiel profile today."),
    ("Google", "gemini", "Gemini 3.8 Flash",
     dict(provider="openai", base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
          model="gemini-3.8-flash", api_key_env="GEMINI_API_KEY",
          context_window=200000, max_output_tokens=32000),
     "Google's long-horizon software-engineering model; 1M context available. "
     "For harder problems try gemini-3.1-pro-preview."),
    ("xAI", "grok", "Grok 4.7",
     dict(provider="openai", base_url="https://api.x.ai/v1", model="grok-4.7",
          api_key_env="XAI_API_KEY", context_window=200000, max_output_tokens=32000),
     "500k context available."),
    # ---------------------------------------------------------------- open-weight clouds
    ("Open-weight models (cloud)", "deepseek", "DeepSeek V4 Pro",
     dict(provider="openai", base_url="https://api.deepseek.com", model="deepseek-v4-pro",
          api_key_env="DEEPSEEK_API_KEY", context_window=200000, max_output_tokens=32000,
          echo_reasoning=True),
     "Text only (deepseek-flash adds vision). Requires reasoning to be sent back on tool turns — "
     "echo_reasoning handles it."),
    ("Open-weight models (cloud)", "kimi", "Moonshot Kimi K3",
     dict(provider="openai", base_url="https://api.moonshot.ai/v1", model="kimi-k3",
          api_key_env="MOONSHOT_API_KEY", context_window=200000, max_output_tokens=32000,
          echo_reasoning=True),
     "Vision + tools. For a code-tuned variant use kimi-k2.7-code (256k)."),
    ("Open-weight models (cloud)", "glm", "Z.ai GLM-5.3",
     dict(provider="openai", base_url="https://api.z.ai/api/paas/v4", model="glm-5.3",
          api_key_env="ZAI_API_KEY", context_window=200000, max_output_tokens=32000),
     "Thinking is always on. Coding Plan subscribers use https://api.z.ai/api/coding/paas/v4 "
     "(double-check the URL in your Z.ai dashboard)."),
    ("Open-weight models (cloud)", "mistral", "Mistral Medium 3.5",
     dict(provider="openai", base_url="https://api.mistral.ai/v1", model="mistral-medium-latest",
          api_key_env="MISTRAL_API_KEY", context_window=200000, max_output_tokens=32000),
     "Mistral's recommended coding model now that Devstral 2 is deprecated."),
    ("Open-weight models (cloud)", "groq", "Groq · gpt-oss 120B (very fast)",
     dict(provider="openai", base_url="https://api.groq.com/openai/v1", model="openai/gpt-oss-120b",
          api_key_env="GROQ_API_KEY", context_window=131072, max_output_tokens=32000),
     "Extremely fast inference; text only."),
    ("Open-weight models (cloud)", "together", "Together AI · Kimi K3",
     dict(provider="openai", base_url="https://api.together.ai/v1", model="moonshotai/Kimi-K3",
          api_key_env="TOGETHER_API_KEY", context_window=200000, max_output_tokens=32000,
          echo_reasoning=True),
     "Also hosts zai-org/GLM-5.3 and deepseek-ai/DeepSeek-V4-Pro-0813."),
    ("Open-weight models (cloud)", "fireworks", "Fireworks AI · Kimi K3",
     dict(provider="openai", base_url="https://api.fireworks.ai/inference/v1",
          model="accounts/fireworks/models/kimi-k3", api_key_env="FIREWORKS_API_KEY",
          context_window=200000, max_output_tokens=32000),
     "Fireworks writes versions with p instead of dots (glm-5p3)."),
    # ---------------------------------------------------------------- routers & enterprise
    ("Routers & enterprise", "openrouter", "OpenRouter (one key, hundreds of models)",
     dict(provider="openai", base_url="https://openrouter.ai/api/v1", model="anthropic/claude-sonnet-5.5",
          api_key_env="OPENROUTER_API_KEY", context_window=200000, max_output_tokens=32000),
     "Model IDs are vendor/model with dots, e.g. openai/gpt-6-astra, moonshotai/kimi-k3."),
    ("Routers & enterprise", "azure", "Azure OpenAI (v1 endpoint)",
     dict(provider="openai", base_url="https://YOUR-RESOURCE.openai.azure.com/openai/v1/",
          model="your-deployment-name", api_key_env="AZURE_OPENAI_API_KEY",
          context_window=200000, max_output_tokens=32000),
     "Replace YOUR-RESOURCE and use your deployment name as the model."),
    ("Routers & enterprise", "bedrock", "AWS Bedrock (OpenAI-compatible) · gpt-oss 120B",
     dict(provider="openai", base_url="https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1",
          model="openai.gpt-oss-120b-1:0", api_key_env="AWS_BEARER_TOKEN_BEDROCK",
          context_window=131072, max_output_tokens=32000),
     "Uses a Bedrock API key as a bearer token; pick your region in the URL."),
]

BY_NAME = {p[1]: p for p in PRESETS}


def as_profile(name: str) -> dict:
    """The preset's fields, ready to write as [models.<name>]."""
    return dict(BY_NAME[name][3])


def toml_block(name: str) -> str:
    from .settings_edit import toml_literal
    group, _, label, fields, note = BY_NAME[name]
    lines = [f"# {label} — {note}", f"[models.{name}]"]
    lines += [f"{k} = {toml_literal(v)}" for k, v in fields.items()]
    return "\n".join(lines) + "\n"


def listing() -> str:
    out, group = [], None
    for g, name, label, fields, _ in PRESETS:
        if g != group:
            out.append(f"\n{g}")
            group = g
        key = f"  (needs ${fields['api_key_env']})" if fields.get("api_key_env") else ""
        out.append(f"  {name:16} {label}{key}")
    return "\n".join(out).lstrip("\n") + "\n\nAdd one: /model add <name> in the TUI, or paste its block from docs/MODELS.md"


def models_markdown() -> str:
    """docs/MODELS.md — generated, keep in sync with `python -m hexiel.presets`."""
    lines = ["# Model presets", "",
             "Ready-to-use profiles for the major providers, checked against each provider's docs on "
             "2026-10-01. Add one from the terminal UI with `/model add <name>`, or paste its block "
             "into `~/.config/hexiel/config.toml`. Then `/model <name>` (or `hexiel -m <name>`).", "",
             "Cloud presets set `context_window = 200000` even where the model supports more: "
             "auto-compaction runs at 82% of the window, so a bigger window means bigger (costlier) "
             "requests. Raise it on purpose for very large codebases.", ""]
    group = None
    for g, name, label, _, note in PRESETS:
        if g != group:
            lines += [f"## {g}", ""]
            group = g
        lines += [f"### `{name}` — {label}", "", note, "", "```toml", toml_block(name).split("\n", 1)[1].rstrip(), "```", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    print(models_markdown())
