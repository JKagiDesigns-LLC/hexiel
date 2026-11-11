"""Plain-language errors: one sentence that says what happened and what to do, instead of a raw
provider reply, a traceback or a bare 'retrying in 122s'. The wording is the doctor's ADVICE.

    explain("provider 401: {...Unauthorized...}", mc)
    → "'glm-5.3-flash:cloud' needs an Ollama Cloud sign-in (401) — run `ollama signin`, or set OLLAMA_API_KEY …"
"""

from __future__ import annotations

import re

_STATUS = re.compile(r"\b(\d{3})\b")


def _local(url: str) -> bool:
    url = (url or "").lower()
    # a substring match against URLs, not a bind address — nosec B104
    return any(h in url for h in ("localhost", "127.0.0.1", "0.0.0.0", "::1", ".local"))  # nosec B104


def explain(error: str, mc=None) -> str:
    """The error, as something a person can act on. Unknown errors come back trimmed, never hidden."""
    text = str(error or "").strip()
    low = text.lower()
    model = getattr(mc, "model", "") or "the model"
    url = getattr(mc, "base_url", "") or ""
    env = getattr(mc, "api_key_env", "") or ""
    provider = getattr(mc, "provider", "") or ""
    status = int(m.group(1)) if (m := _STATUS.search(text[:40])) else 0
    local = _local(url)
    ollama = provider != "anthropic" and ("ollama" in url or ":cloud" in model or model.endswith("-cloud") or ":11434" in url)

    if any(w in low for w in ("connecterror", "connection refused", "connect call failed", "errno 111", "name or service not known",
                              "nodename nor servname", "failed to establish", "all connection attempts failed")):
        if local:
            return (f"the model server at {url or 'localhost:11434'} is not running — start it with `ollama serve` "
                    "(or open the Ollama app), then send your message again")
        return f"could not reach {url or provider} — check the URL in config.toml and your network, then try again"
    if "timed out" in low or "readtimeout" in low or "timeout" in low:
        return (f"{model} accepted the request but did not answer in time — the first request loads the model "
                "(slow once) or the server is busy; hexiel keeps retrying, Esc Esc stops it")
    if status == 401 or "unauthorized" in low or "invalid api key" in low or "authentication" in low:
        if ollama:
            return (f"'{model}' needs an Ollama Cloud sign-in (401) — run `ollama signin`, or get a key at "
                    "https://ollama.com/settings/keys and run `hexiel auth set OLLAMA_API_KEY`")
        return (f"the {provider or 'provider'} key was rejected (401) — "
                + (f"check {env}: `hexiel auth set {env}` stores a new one" if env else "check the key in config.toml"))
    if status == 403:
        return f"the {provider or 'provider'} refused this key for '{model}' (403) — the key may lack access to that model"
    if status == 404 or "not found" in low and "model" in low:
        if local:
            return f"'{model}' is not on your Ollama — `ollama pull {model}`, or pick one that is there with /model"
        return f"'{model}' does not exist at {url or provider} (404) — pick another with /model"
    if status == 402 or "credit balance" in low or "insufficient_quota" in low or "billing" in low:
        return f"the {provider or 'provider'} account is out of credit for '{model}' — add credits or switch models (/model)"
    if status == 529 or "overloaded" in low:
        return (f"the {provider or 'model'} servers are overloaded right now (not your account) — hexiel retries; "
                "a fallback profile would answer meanwhile")
    if status == 429 or "rate limit" in low:
        return f"'{model}' is rate-limited right now — hexiel waits and retries by itself; a fallback profile would skip the wait"
    if status in (500, 502, 503, 529):
        return f"the {provider or 'model'} server had an internal error ({status}) — hexiel retries; if it keeps failing, try /model"
    if "too long" in low or ("context" in low and ("length" in low or "window" in low)):
        return "the conversation no longer fits the model's context window — /compact, or /clear for a fresh start"
    if "ssl" in low or "certificate" in low:
        return f"the TLS certificate of {url or provider} could not be verified — a proxy or clock problem on this machine"
    return text[:300]


def crash(exc: BaseException, saved: bool = True) -> str:
    """What a person sees when hexiel itself fails mid-turn."""
    what = f"{type(exc).__name__}: {str(exc)[:160]}" if str(exc) else type(exc).__name__
    where = " It was saved as an incident — run /heal to reproduce and fix it, or /bug to report it." if saved else ""
    return f"something went wrong inside hexiel ({what}).{where} Your conversation is kept; send your message again."
