"""Live model discovery for the model picker — always asked fresh, never cached.

Every reachable endpoint is queried in parallel when the picker opens:
  * each configured profile's OpenAI-compatible server (GET {base_url}/models —
    Ollama, LM Studio, vLLM, OpenAI, OpenRouter, Groq, Gemini, DeepSeek, …),
  * Ollama servers additionally via /api/tags (sizes; ":cloud" = Ollama Cloud),
  * Anthropic via GET /v1/models,
  * presets whose API-key variable is set in the environment, even with no
    profile yet (so a newly exported key shows its models immediately).
Results stream back per endpoint through a callback, so the picker fills in as
providers answer; unreachable ones report why."""

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import httpx

TIMEOUT = 6.0


@dataclass
class Endpoint:
    label: str                 # shown as the group header
    provider: str              # "openai" | "anthropic"
    base_url: str
    api_key_env: str = ""
    template: dict = field(default_factory=dict)   # profile fields for new entries
    profiles: dict = field(default_factory=dict)   # model id -> existing profile name


@dataclass
class Found:
    endpoint: Endpoint
    model: str
    profile: str = ""          # existing profile for this model, if any
    detail: str = ""           # size / "cloud" / context …


def endpoints(cfg) -> list[Endpoint]:
    """Distinct endpoints from the profiles plus key-bearing presets."""
    from .presets import PRESETS
    seen: dict[tuple, Endpoint] = {}

    def add(label, fields, profile_name=""):
        provider = fields.get("provider", "openai")
        base = (fields.get("base_url") or ("https://api.anthropic.com" if provider == "anthropic" else "")).rstrip("/")
        if not base:
            return
        key = (provider, base, fields.get("api_key_env", ""))
        ep = seen.get(key)
        if ep is None:
            template = {k: v for k, v in fields.items() if k not in ("model",)}
            ep = seen[key] = Endpoint(label=label, provider=provider, base_url=base,
                                      api_key_env=fields.get("api_key_env", ""), template=template)
        if profile_name:
            ep.profiles[fields.get("model", "")] = profile_name

    for name, mc in cfg.models.items():
        fields = {"provider": mc.provider, "base_url": mc.base_url, "model": mc.model,
                  "api_key_env": mc.api_key_env, "context_window": mc.context_window,
                  "max_output_tokens": mc.max_output_tokens, "extra_body": mc.extra_body,
                  "echo_reasoning": mc.echo_reasoning}
        add(_label(mc.provider, mc.base_url), fields, name)
    for group, name, label, fields, _ in PRESETS:
        env = fields.get("api_key_env")
        if env and os.environ.get(env):            # connected via an exported key
            add(_label(fields.get("provider", "openai"), fields.get("base_url", "")), dict(fields))
    return list(seen.values())


def _label(provider: str, base_url: str) -> str:
    if provider == "anthropic":
        return "Anthropic"
    host = httpx.URL(base_url).host if base_url else ""
    known = {"api.openai.com": "OpenAI", "openrouter.ai": "OpenRouter", "api.groq.com": "Groq",
             "generativelanguage.googleapis.com": "Google Gemini", "api.x.ai": "xAI",
             "api.deepseek.com": "DeepSeek", "api.mistral.ai": "Mistral", "api.moonshot.ai": "Moonshot Kimi",
             "api.z.ai": "Z.ai GLM", "api.together.ai": "Together AI", "api.fireworks.ai": "Fireworks AI"}
    if host in known:
        return known[host]
    port = httpx.URL(base_url).port
    if port == 11434:
        return f"Ollama @ {host}"
    return f"{host}:{port}" if port else host


def _headers(ep: Endpoint) -> dict:
    key = os.environ.get(ep.api_key_env, "") if ep.api_key_env else ""
    if ep.provider == "anthropic":
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    return {"authorization": f"Bearer {key}"} if key else {}


def query(ep: Endpoint) -> list[Found]:
    """Ask one endpoint what it serves right now. Raises on failure."""
    with httpx.Client(timeout=TIMEOUT) as client:
        if ep.provider == "anthropic":
            if not os.environ.get(ep.api_key_env or "ANTHROPIC_API_KEY"):
                raise RuntimeError(f"${ep.api_key_env or 'ANTHROPIC_API_KEY'} is not set")
            resp = client.get(f"{ep.base_url}/v1/models", params={"limit": 100}, headers=_headers(ep))
            resp.raise_for_status()
            return [Found(ep, m["id"], ep.profiles.get(m["id"], ""), m.get("display_name", ""))
                    for m in resp.json().get("data", [])]
        native = ep.base_url[:-3] if ep.base_url.endswith("/v1") else ""
        if native and ":11434" in ep.base_url:          # Ollama: richer listing
            resp = client.get(f"{native}/api/tags")
            resp.raise_for_status()
            out = []
            for m in resp.json().get("models", []):
                cloud = m.get("remote_host") or m["name"].endswith(":cloud") or m["name"].endswith("-cloud")
                size = m.get("size") or 0
                human = f"{size / 1e9:.0f} GB" if size >= 1e9 else f"{size / 1e6:.0f} MB"
                detail = "Ollama Cloud" if cloud else (f"{human} local" if size else "local")
                out.append(Found(ep, m["name"], ep.profiles.get(m["name"], ""), detail))
            return out
        resp = client.get(f"{ep.base_url}/models", headers=_headers(ep))
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data", data.get("models", [])) if isinstance(data, dict) else data
        out = []
        for m in items:
            mid = m.get("id") or m.get("name", "")
            mid = mid.removeprefix("models/")              # Gemini lists "models/gemini-…"
            if mid:
                out.append(Found(ep, mid, ep.profiles.get(mid, ""),
                                 f"{m['context_length'] // 1000}k ctx" if m.get("context_length") else ""))
        return out


def discover(cfg, on_result) -> None:
    """Query every endpoint in parallel; on_result(endpoint, found_list, error)
    is called once per endpoint as soon as it answers."""
    eps = endpoints(cfg)
    if not eps:
        return

    def one(ep):
        try:
            on_result(ep, query(ep), "")
        except Exception as exc:  # noqa: BLE001 - report, don't fail the picker
            msg = str(exc).split("\n")[0][:120] or type(exc).__name__
            on_result(ep, [], msg)
    with ThreadPoolExecutor(max_workers=min(12, len(eps))) as pool:
        list(pool.map(one, eps))


def profile_for(found: Found) -> tuple[str, dict]:
    """(profile name, fields) for a discovered model — the existing profile when
    there is one, else a new one derived from the endpoint's template."""
    if found.profile:
        return found.profile, {}
    import re
    fields = dict(found.endpoint.template)
    fields["model"] = found.model
    fields.setdefault("provider", found.endpoint.provider)
    if found.endpoint.provider == "openai":
        fields["base_url"] = found.endpoint.base_url
    fields.setdefault("context_window", 32768 if ":11434" in found.endpoint.base_url else 200000)
    fields.setdefault("max_output_tokens", 8192)
    name = re.sub(r"[^a-z0-9.-]+", "-", found.model.lower().split("/")[-1]).strip("-")[:40]
    return name, fields
