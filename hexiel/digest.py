"""Per-tool models: let a small, fast model read for the main one.

`[tool_models]` in config.toml maps a tool to a model profile, e.g.

    [tool_models]
    read = "small-local"      # a quick model summarises big files before the main model sees them
    grep = "small-local"
    web_fetch = "small-local"
    search = "strong-local"   # alias for web_search

When a mapped tool returns more than `digest_min_chars` (default 3000), that model condenses the output for the current
request, and the main model gets the digest with a note — it can ask for the raw output by calling the tool again with
`full: true`. Small outputs, errors and images pass through untouched, and any failure falls back to the raw output.
Tools that *author* (write, edit, bash) are never delegated: the main model has to write its own changes. For planning,
set `planner_profile` — that is the plan tool's model."""

from . import modelbusy, planner

DIGESTIBLE = ("read", "grep", "glob", "list", "map", "web_fetch", "web_search")
ALIASES = {"search": "web_search", "fetch": "web_fetch", "files": "glob"}
MAX_INPUT = 60000
SYSTEM = ("You condense tool output for a coding agent that is working on a request. Keep everything the agent needs: exact "
          "identifiers, file paths, line numbers (keep the `N:` prefixes of lines you quote), error messages, versions, "
          "numbers and short code excerpts that matter. Drop boilerplate, repetition and unrelated sections. Be factual; "
          "never invent. Under 350 words. If the output looks unrelated to the request, say what it contains in two sentences.")


def mapping(settings) -> dict:
    """tool name -> profile name, with aliases resolved and unsupported tools dropped."""
    out = {}
    for key, profile in (getattr(settings, "tool_models", None) or {}).items():
        tool = ALIASES.get(str(key).lower(), str(key).lower())
        if tool in DIGESTIBLE and profile:
            out[tool] = str(profile)
    return out


def model_for(session, tool_name: str):
    """The model that digests this tool's output, or None (not mapped, unknown profile, key missing, or it is the model
    already in use — nothing to gain)."""
    profile = mapping(session.settings).get(tool_name)
    if not profile:
        return None
    mc = planner._config_profile(session, profile)
    if mc is None or mc.name == session.model_config.name:
        return None
    import os
    if mc.api_key_env and not os.environ.get(mc.api_key_env) and mc.provider != "anthropic":
        return None
    return mc


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


def maybe_digest(session, call: dict, result):
    """The tool result the main model should see (possibly condensed by the tool's model)."""
    if result.is_error or getattr(result, "images", None) or (call.get("arguments") or {}).get("full") is True:
        return result
    mc = model_for(session, call["name"])
    limit = int(getattr(session.settings, "digest_min_chars", 3000))
    raw = result.text or ""
    if mc is None or len(raw) <= limit:
        return result
    request = (session.__dict__.get("_turn_request", "") or "")[:2000]
    user = (f"## The request\n{request or '(not stated)'}\n\n## Tool\n{call['name']} {call.get('arguments')}\n\n"
            f"## Output ({len(raw):,} characters)\n{raw[:MAX_INPUT]}")
    slot = modelbusy.enter(mc, lambda t: session.on_event("note", t), "digest")
    session.set_active_model(mc, "summarizing")
    try:
        text, _usage = planner.complete(session, mc, SYSTEM, user, max_tokens=800)
    except Exception as exc:  # noqa: BLE001 - the raw output is always a safe fallback
        session.on_event("note", f"↩ {mc.name} couldn't summarise the {call['name']} output ({type(exc).__name__}) — using it as is")
        return result
    finally:
        slot.release()
        session.active_model = ""
        session.on_event("model_active")
    if not text or len(text) >= len(raw) * 0.8:
        return result
    session.on_event("note", f"🔎 {call['name']} output ({len(raw):,} chars) summarised by {mc.name}")
    from .tools import ToolResult
    return ToolResult(text=f"[summary by {mc.name} of {len(raw):,} characters — call {call['name']} again with full=true "
                           f"for the raw output]\n{text}", is_error=False)
