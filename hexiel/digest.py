"""Per-tool models: let a small, fast model read for the main one.

`[tool_models]` in config.toml maps a tool to a model profile, e.g.

    [tool_models]
    read = "small-local"      # a quick model summarises big files before the main model sees them
    grep = "small-local"
    web_fetch = "small-local"
    search = "strong-local"   # alias for web_search

A value can also be a list (try these in order, preferring one that is not busy with another request) or a table with
per-tool limits — useful for a slow helper such as a Raspberry Pi, which should only see small inputs:

    read = { models = ["pi", "gmktec"], min_chars = 1500, max_input = 6000, timeout = 100, max_tokens = 220 }

When a mapped tool returns more than `min_chars` (default `digest_min_chars`, 3000), that model condenses the output for
the current request, and the main model gets the digest with a note — it can ask for the raw output by calling the tool
again with `full: true`. Outputs bigger than `max_input` are not sent at all (the raw text is used), small outputs, errors
and images pass through untouched, and any failure or timeout falls back to the raw output.
Tools that *author* (write, edit, bash) are never delegated: the main model has to write its own changes. For planning,
set `planner_profile` — that is the plan tool's model."""

import hashlib
import time

from . import modelbusy, planner

DIGESTIBLE = ("read", "grep", "glob", "list", "map", "web_fetch", "web_search")
ALIASES = {"search": "web_search", "fetch": "web_fetch", "files": "glob"}
MAX_INPUT = 60000
MAX_OUTPUT_TOKENS = 500       # a slow helper spends most of its time writing: a shorter digest is a faster one
SYSTEM = ("You condense tool output for a coding agent that is working on a request. Keep everything the agent needs: exact "
          "identifiers, file paths, line numbers (keep the `N:` prefixes of lines you quote), error messages, versions, "
          "numbers and short code excerpts that matter. Drop boilerplate, repetition and unrelated sections. Be factual; "
          "never invent. Under {words} words. If the output looks unrelated to the request, say what it contains in two sentences.")


def rules(settings) -> dict:
    """tool name -> {"models": [profile, …], "min_chars", "max_input", "timeout"} (None = use the global setting)."""
    out = {}
    for key, value in (getattr(settings, "tool_models", None) or {}).items():
        tool = ALIASES.get(str(key).lower(), str(key).lower())
        if tool not in DIGESTIBLE:
            continue
        if isinstance(value, dict):
            models = value.get("models", value.get("model", []))
            extra = {k: value.get(k) for k in ("min_chars", "max_input", "timeout", "max_tokens")}
        else:
            models, extra = value, {}
        models = [models] if isinstance(models, str) else list(models or [])
        models = [str(m) for m in models if m]
        if models:
            out[tool] = {"models": models, **{k: (int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None)
                                              for k, v in {"min_chars": extra.get("min_chars"), "max_input": extra.get("max_input"),
                                                           "timeout": extra.get("timeout"), "max_tokens": extra.get("max_tokens")}.items()}}
    return out


def mapping(settings) -> dict:
    """tool name -> its first profile name (what `/toolmodels` and the tool schema care about)."""
    return {tool: r["models"][0] for tool, r in rules(settings).items()}


def usable(session, profile: str):
    """The ModelConfig for a profile if it can be used for a digest right now, else None."""
    import os
    mc = planner._config_profile(session, profile)
    if mc is None or mc.name == session.model_config.name:
        return None
    if mc.api_key_env and not os.environ.get(mc.api_key_env) and mc.provider != "anthropic":
        return None
    return mc


def model_for(session, tool_name: str):
    """The model that digests this tool's output, or None (not mapped, unknown profile, key missing, or it is the model
    already in use — nothing to gain). With several candidates, the first one not busy with another request wins."""
    rule = rules(session.settings).get(tool_name)
    if not rule:
        return None
    candidates = [mc for mc in (usable(session, p) for p in rule["models"]) if mc is not None]
    for mc in candidates:
        if not (modelbusy.is_local(mc) and modelbusy.others(mc)):
            return mc
    return candidates[0] if candidates else None


def add_full_param(tool_defs: list, settings) -> list:
    """Mapped tools get an optional `full` argument so the model can ask for the raw output."""
    mapped = mapping(settings)
    if not mapped:
        return tool_defs
    out = []
    for d in tool_defs:
        if d["name"] in mapped:
            params = dict(d["parameters"])
            props = dict(params.get("properties", {}))
            props["full"] = {"type": "boolean", "description": "true = the raw output, not a summary by a helper model"}
            params["properties"] = props
            d = {**d, "parameters": params}
        out.append(d)
    return out


def stats(session) -> dict:
    return session.__dict__.setdefault("_digest_stats", {"digested": 0, "cached": 0, "raw_chars": 0, "kept_chars": 0,
                                                         "fallbacks": 0, "seconds": 0.0})


def summary_line(session) -> str:
    """For /usage: what the tool models did this session ('' when they did nothing)."""
    st = stats(session)
    if not (st["digested"] or st["cached"] or st["fallbacks"]):
        return ""
    saved = st["raw_chars"] - st["kept_chars"]
    return (f"tool models: {st['digested']} output(s) condensed ({st['cached']} from cache), ~{saved // 4:,} tokens kept out of the main "
            f"model's context, {st['seconds']:.0f}s spent" + (f", {st['fallbacks']} fell back to the raw text" if st["fallbacks"] else ""))


def maybe_digest(session, call: dict, result):
    """The tool result the main model should see (possibly condensed by the tool's model)."""
    if result.is_error or getattr(result, "images", None) or (call.get("arguments") or {}).get("full") is True:
        return result
    mc = model_for(session, call["name"])
    rule = rules(session.settings).get(call["name"]) or {}
    limit = rule.get("min_chars") or int(getattr(session.settings, "digest_min_chars", 3000))
    max_input = rule.get("max_input") or MAX_INPUT
    budget = float(rule.get("timeout") or getattr(session.settings, "digest_timeout", 90))
    max_tokens = int(rule.get("max_tokens") or MAX_OUTPUT_TOKENS)
    raw = result.text or ""
    if mc is None or len(raw) <= limit or len(raw) > max_input:
        return result
    from .tools import ToolResult
    st = stats(session)
    request = (session.__dict__.get("_turn_request", "") or "")[:2000]
    cache = session.__dict__.setdefault("_digest_cache", {})
    key = (call["name"], mc.name, hashlib.sha1(f"{request}\n{raw}".encode(), usedforsecurity=False).hexdigest())
    text = cache.get(key)
    if text is None:
        user = (f"## The request\n{request or '(not stated)'}\n\n## Tool\n{call['name']} {call.get('arguments')}\n\n"
                f"## Output ({len(raw):,} characters)\n{raw}")
        slot = modelbusy.enter(mc, lambda t: session.on_event("note", t), "digest")
        session.set_active_model(mc, "summarizing")
        began = time.time()
        try:
            text, _usage = planner.complete(session, mc, SYSTEM.format(words=max(40, int(max_tokens * 0.7))), user,
                                            max_tokens=max_tokens, budget=budget)
        except Exception as exc:  # noqa: BLE001 - the raw output is always a safe fallback
            st["fallbacks"] += 1
            why = "took too long" if isinstance(exc, (TimeoutError, OSError)) or "Timeout" in type(exc).__name__ else type(exc).__name__
            session.on_event("note", f"↩ {mc.name} couldn't summarise the {call['name']} output ({why}) — using it as is")
            return result
        finally:
            slot.release()
            session.active_model = ""
            session.on_event("model_active")
            st["seconds"] += time.time() - began
        if not text or len(text) >= len(raw) * 0.8:
            return result
        cache[key] = text
        st["digested"] += 1
    else:
        st["cached"] += 1
    st["raw_chars"] += len(raw)
    st["kept_chars"] += len(text)
    session.on_event("note", f"🔎 {call['name']} output ({len(raw):,} chars) summarised by {mc.name}")
    return ToolResult(text=f"[summary by {mc.name} of {len(raw):,} characters — call {call['name']} again with full=true "
                           f"for the raw output]\n{text}", is_error=False)


def status_text(session, probe: bool = False) -> str:
    """/toolmodels: which tools use which model, whether that model is usable, and (with probe) how fast it answers."""
    import os
    rs = rules(session.settings)
    if not rs:
        return ("no tool models set. Add a [tool_models] table to config.toml, e.g.\n  [tool_models]\n  read = \"small-local\"\n"
                "  grep = \"small-local\"\nsee the README (\"A small model for the small jobs\").")
    lines, seen = [], {}
    st = session.settings
    for tool, rule in sorted(rs.items()):
        parts = []
        for profile in rule["models"]:
            mc = planner._config_profile(session, profile)
            if mc is None:
                state = "✗ no such model profile"
            elif mc.name == session.model_config.name:
                state = "= the model you are already using"
            elif mc.api_key_env and not os.environ.get(mc.api_key_env) and mc.provider != "anthropic":
                state = f"✗ {mc.api_key_env} is not set"
            else:
                busy = modelbusy.others(mc) if modelbusy.is_local(mc) else 0
                state = "✓ ready" + (f", busy with {busy} other request(s)" if busy else "")
                if probe and mc.name not in seen:
                    began = time.time()
                    try:
                        planner.complete(session, mc, "Answer in one word.", "Say ok.", max_tokens=8, budget=60)
                        seen[mc.name] = f"answered in {time.time() - began:.1f}s"
                    except Exception as exc:  # noqa: BLE001
                        seen[mc.name] = f"✗ {type(exc).__name__}: {str(exc)[:80]}"
                if mc.name in seen:
                    state += f" — {seen[mc.name]}"
            parts.append(f"{profile} ({state})")
        limits = (f"over {rule['min_chars'] or st.digest_min_chars:,} chars"
                  + (f", up to {rule['max_input']:,}" if rule["max_input"] else "")
                  + f", {rule['timeout'] or st.digest_timeout}s, ≤{rule['max_tokens'] or MAX_OUTPUT_TOKENS} tokens out")
        lines.append(f"  {tool:<10} → {' then '.join(parts)}  [{limits}]")
    extra = summary_line(session)
    return "tool models:\n" + "\n".join(lines) + (f"\n{extra}" if extra else "")
