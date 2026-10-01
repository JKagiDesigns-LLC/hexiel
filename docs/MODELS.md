# Model presets

Ready-to-use profiles for the major providers, checked against each provider's docs on 2026-10-01. Add one from the terminal UI with `/model add <name>`, or paste its block into `~/.config/hexiel/config.toml`. Then `/model <name>` (or `hexiel -m <name>`).

Cloud presets set `context_window = 200000` even where the model supports more: auto-compaction runs at 82% of the window, so a bigger window means bigger (costlier) requests. Raise it on purpose for very large codebases.

## Local (free, private)

### `ollama-qwen` — Ollama · Qwen3.6 35B-A3B coding (vision + tools, fast MoE)

Best all-round local model on modest hardware (~25 GB). Match context_window to the server's OLLAMA_CONTEXT_LENGTH. `ollama pull qwen3.6:35b-a3b-coding`.

```toml
[models.ollama-qwen]
provider = "openai"
base_url = "http://localhost:11434/v1"
model = "qwen3.6:35b-a3b-coding"
context_window = 65536
max_output_tokens = 8192
extra_body = { reasoning_effort = "none" }
```

### `ollama-gpt-oss` — Ollama · gpt-oss 20B (tools, small)

Fits ~16 GB machines; text only. `ollama pull gpt-oss:20b`.

```toml
[models.ollama-gpt-oss]
provider = "openai"
base_url = "http://localhost:11434/v1"
model = "gpt-oss:20b"
context_window = 65536
max_output_tokens = 8192
```

### `lmstudio` — LM Studio (whatever model you have loaded)

Set `model` to the identifier LM Studio shows for the loaded model.

```toml
[models.lmstudio]
provider = "openai"
base_url = "http://localhost:1234/v1"
model = "local-model"
context_window = 32768
max_output_tokens = 8192
```

### `vllm` — vLLM server

Start vLLM with tool calling enabled (--enable-auto-tool-choice + a tool parser).

```toml
[models.vllm]
provider = "openai"
base_url = "http://localhost:8000/v1"
model = "your/served-model"
context_window = 65536
max_output_tokens = 8192
```

### `llamacpp` — llama.cpp llama-server

Run llama-server with --jinja (needed for tool calls).

```toml
[models.llamacpp]
provider = "openai"
base_url = "http://127.0.0.1:8080/v1"
model = "local"
context_window = 32768
max_output_tokens = 8192
```

## Anthropic

### `sonnet` — Claude Sonnet 5.5

Great default for hard coding work. Supports 1M context (raise context_window to use it).

```toml
[models.sonnet]
provider = "anthropic"
model = "claude-sonnet-5-5"
api_key_env = "ANTHROPIC_API_KEY"
context_window = 200000
max_output_tokens = 32000
```

### `opus` — Claude Opus 5.5

Strongest Claude for complex, long tasks. Supports 1M context.

```toml
[models.opus]
provider = "anthropic"
model = "claude-opus-5-5"
api_key_env = "ANTHROPIC_API_KEY"
context_window = 200000
max_output_tokens = 32000
```

### `haiku` — Claude Haiku 4.5

Fast and cheap.

```toml
[models.haiku]
provider = "anthropic"
model = "claude-haiku-4-5"
api_key_env = "ANTHROPIC_API_KEY"
context_window = 200000
max_output_tokens = 32000
```

## OpenAI

### `gpt` — OpenAI GPT-6 Astra

Tool calling over Chat Completions. Note: gpt-6.1-sol has no tools on Chat Completions and gpt-5.3-codex is Responses-API-only — neither works as a hexiel profile today.

```toml
[models.gpt]
provider = "openai"
base_url = "https://api.openai.com/v1"
model = "gpt-6-astra"
api_key_env = "OPENAI_API_KEY"
context_window = 200000
max_output_tokens = 32000
```

## Google

### `gemini` — Gemini 3.8 Flash

Google's long-horizon software-engineering model; 1M context available. For harder problems try gemini-3.1-pro-preview.

```toml
[models.gemini]
provider = "openai"
base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
model = "gemini-3.8-flash"
api_key_env = "GEMINI_API_KEY"
context_window = 200000
max_output_tokens = 32000
```

## xAI

### `grok` — Grok 4.7

500k context available.

```toml
[models.grok]
provider = "openai"
base_url = "https://api.x.ai/v1"
model = "grok-4.7"
api_key_env = "XAI_API_KEY"
context_window = 200000
max_output_tokens = 32000
```

## Open-weight models (cloud)

### `deepseek` — DeepSeek V4 Pro

Text only (deepseek-flash adds vision). Requires reasoning to be sent back on tool turns — echo_reasoning handles it.

```toml
[models.deepseek]
provider = "openai"
base_url = "https://api.deepseek.com"
model = "deepseek-v4-pro"
api_key_env = "DEEPSEEK_API_KEY"
context_window = 200000
max_output_tokens = 32000
echo_reasoning = true
```

### `kimi` — Moonshot Kimi K3

Vision + tools. For a code-tuned variant use kimi-k2.7-code (256k).

```toml
[models.kimi]
provider = "openai"
base_url = "https://api.moonshot.ai/v1"
model = "kimi-k3"
api_key_env = "MOONSHOT_API_KEY"
context_window = 200000
max_output_tokens = 32000
echo_reasoning = true
```

### `glm` — Z.ai GLM-5.3

Thinking is always on. Coding Plan subscribers use https://api.z.ai/api/coding/paas/v4 (double-check the URL in your Z.ai dashboard).

```toml
[models.glm]
provider = "openai"
base_url = "https://api.z.ai/api/paas/v4"
model = "glm-5.3"
api_key_env = "ZAI_API_KEY"
context_window = 200000
max_output_tokens = 32000
```

### `mistral` — Mistral Medium 3.5

Mistral's recommended coding model now that Devstral 2 is deprecated.

```toml
[models.mistral]
provider = "openai"
base_url = "https://api.mistral.ai/v1"
model = "mistral-medium-latest"
api_key_env = "MISTRAL_API_KEY"
context_window = 200000
max_output_tokens = 32000
```

### `groq` — Groq · gpt-oss 120B (very fast)

Extremely fast inference; text only.

```toml
[models.groq]
provider = "openai"
base_url = "https://api.groq.com/openai/v1"
model = "openai/gpt-oss-120b"
api_key_env = "GROQ_API_KEY"
context_window = 131072
max_output_tokens = 32000
```

### `together` — Together AI · Kimi K3

Also hosts zai-org/GLM-5.3 and deepseek-ai/DeepSeek-V4-Pro-0813.

```toml
[models.together]
provider = "openai"
base_url = "https://api.together.ai/v1"
model = "moonshotai/Kimi-K3"
api_key_env = "TOGETHER_API_KEY"
context_window = 200000
max_output_tokens = 32000
echo_reasoning = true
```

### `fireworks` — Fireworks AI · Kimi K3

Fireworks writes versions with p instead of dots (glm-5p3).

```toml
[models.fireworks]
provider = "openai"
base_url = "https://api.fireworks.ai/inference/v1"
model = "accounts/fireworks/models/kimi-k3"
api_key_env = "FIREWORKS_API_KEY"
context_window = 200000
max_output_tokens = 32000
```

## Routers & enterprise

### `openrouter` — OpenRouter (one key, hundreds of models)

Model IDs are vendor/model with dots, e.g. openai/gpt-6-astra, moonshotai/kimi-k3.

```toml
[models.openrouter]
provider = "openai"
base_url = "https://openrouter.ai/api/v1"
model = "anthropic/claude-sonnet-5.5"
api_key_env = "OPENROUTER_API_KEY"
context_window = 200000
max_output_tokens = 32000
```

### `azure` — Azure OpenAI (v1 endpoint)

Replace YOUR-RESOURCE and use your deployment name as the model.

```toml
[models.azure]
provider = "openai"
base_url = "https://YOUR-RESOURCE.openai.azure.com/openai/v1/"
model = "your-deployment-name"
api_key_env = "AZURE_OPENAI_API_KEY"
context_window = 200000
max_output_tokens = 32000
```

### `bedrock` — AWS Bedrock (OpenAI-compatible) · gpt-oss 120B

Uses a Bedrock API key as a bearer token; pick your region in the URL.

```toml
[models.bedrock]
provider = "openai"
base_url = "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1"
model = "openai.gpt-oss-120b-1:0"
api_key_env = "AWS_BEARER_TOKEN_BEDROCK"
context_window = 131072
max_output_tokens = 32000
```

