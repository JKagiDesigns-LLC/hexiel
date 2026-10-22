"""Read and change hexiel settings at runtime — for this session only, or
permanently in config.toml. Shared by the TUI (/config) and the web UI.

Permanent edits rewrite single lines of config.toml (never the whole file),
so comments and layout survive; every edit is re-parsed and verified before
it is written."""

import dataclasses
import json
import re
import shutil

from . import config as config_mod

# Editable fields. `options` is a list of choices, or None for free text.
FIELDS = [
    {"key": "profile", "label": "Model profile", "kind": "choice",
     "help": "which [models.*] profile is used (permanent = the startup default)"},
    {"key": "model.model", "label": "Model", "kind": "choice",
     "help": "model id for the current profile (live list from its server)"},
    {"key": "model.reasoning", "label": "Thinking", "kind": "choice",
     "options": ["none", "low", "medium", "high", "model default"],
     "help": "reasoning_effort sent to the server — 'none' is fastest on slow hardware"},
    {"key": "model.context_window", "label": "Context window", "kind": "number",
     "options": [32768, 65536, 131072, 200000, 262144],
     "help": "tokens before auto-compaction; keep in sync with the server's limit"},
    {"key": "model.max_output_tokens", "label": "Max output", "kind": "number",
     "options": [4096, 8192, 16384, 32768], "help": "longest single reply"},
    {"key": "settings.auto_approve", "label": "Auto-approve", "kind": "choice",
     "options": ["off", "except commits", "everything"],
     "help": "off = ask first; except commits = run everything but ask before git/svn commits and pushes; "
             "everything = never ask (risky)"},
    {"key": "settings.compact_threshold", "label": "Compact at", "kind": "number",
     "options": [0.7, 0.75, 0.8, 0.82, 0.85, 0.9],
     "help": "share of the context window that triggers auto-compaction"},
    {"key": "settings.compact_profile", "label": "Summary model", "kind": "choice",
     "help": "model that writes compaction summaries — a fast cloud model takes seconds, a slow local one many minutes; "
             "empty = the current model"},
    {"key": "settings.cloud_fallback_profile", "label": "Cloud fallback", "kind": "choice",
     "help": "used only for cloud tasks (plans, advice, summaries) when the planner/summary model fails or is rate-limited; empty = none"},
    {"key": "settings.request_timeout", "label": "Request timeout", "kind": "number",
     "options": [300, 600, 900, 1800, 3600], "help": "seconds per model request"},
    {"key": "settings.auto_resume", "label": "Auto-resume", "kind": "bool",
     "options": [True, False], "help": "wait out provider rate/usage limits and continue automatically"},
    {"key": "settings.fallback_profile", "label": "Fallback model", "kind": "choice",
     "help": "profile to switch to when a limit won't reset soon (e.g. your local model); empty = none"},
    {"key": "settings.subagent_profile", "label": "Subagent model", "kind": "choice",
     "help": "profile for `task` subagents — e.g. a cheaper or local model; empty = current model"},
    {"key": "settings.planner_profile", "label": "Planner model", "kind": "choice",
     "help": "/delegate: the strong model that writes the plan (e.g. a cloud model); empty = fallback model, else the current one"},
    {"key": "settings.executor_profile", "label": "Worker model", "kind": "choice",
     "help": "/delegate: the model that does each step (e.g. your local model); empty = the current model"},
    {"key": "settings.workers", "label": "Parallel steps", "kind": "choice",
     "options": ["auto", 1, 2, 4, 8],
     "help": "/delegate: independent steps run at the same time — auto = every CPU core but one"},
    {"key": "settings.worker_fallback_profile", "label": "Worker when busy", "kind": "choice",
     "help": "/delegate: model used for a step while the worker model is busy or not answering; empty = always wait for the worker"},
    {"key": "settings.router_profile", "label": "Router", "kind": "choice",
     "help": "a small cloud model that sorts each message before the main model runs it: chats answered directly, "
             "small tasks to the main model, big ones planned first (e.g. claude-haiku); empty = every message goes "
             "straight to the main model"},
    {"key": "settings.overflow_profile", "label": "Overflow when busy", "kind": "choice",
     "help": "chat-sized requests run here while the main model is busy with another request, instead of waiting "
             "(e.g. claude-haiku); empty = always wait"},
    {"key": "settings.overflow_daily_tokens", "label": "Daily cloud cap", "kind": "number",
     "options": [0, 50000, 100000, 200000, 500000, 1000000],
     "help": "tokens per day the router model may spend (routing, chat answers, overflow turns), then requests wait "
             "for the main model again; 0 = no cap"},
    {"key": "settings.auto_delegate", "label": "Big-task check", "kind": "choice",
     "options": ["off", "ask", "auto"],
     "help": "size up each request: off, ask before delegating big ones, or delegate them automatically"},
    {"key": "settings.delegate_approval", "label": "Approve plans", "kind": "bool",
     "options": [True, False], "help": "show the plan and wait for a yes before the worker model starts"},
    {"key": "settings.delegate_retries", "label": "Step retries", "kind": "number",
     "options": [0, 1, 2, 3, 5], "help": "fix attempts per step before it is handed to the planner model"},
    {"key": "settings.escalate_mode", "label": "Switch to cloud", "kind": "choice",
     "options": ["auto", "off"],
     "help": "when the working model keeps struggling (failures, empty results, repeats, or you correcting it) the planner "
             "model takes over for a few steps, then the work goes back; /cloud forces it for one request"},
    {"key": "settings.consult_mode", "label": "Ask for advice", "kind": "choice",
     "options": ["auto", "tool", "off"],
     "help": "the planner model gives advice when the working model asks, or (auto) when it is stuck — "
             "3 failing tool calls in a row or the same call 3 times"},
    {"key": "settings.delegate_review", "label": "Review plans", "kind": "choice",
     "options": ["auto", "always", "off"],
     "help": "after a /delegate run the planner model reviews the diff and the worker fixes what it finds; "
             "auto = only when the planner is a different model"},
    {"key": "settings.error_reports", "label": "Send error reports", "kind": "choice",
     "options": ["account", "all", "off"],
     "help": "hexiel's own errors (paths, keys and e-mails scrubbed) go to remote.hexiel.tech so they can be tracked: "
             "account = only when signed in to Hexiel Remote (see them at hexiel.tech/support), all = also without an "
             "account (public queue), off = never. /bug always sends"},
    {"key": "settings.auto_commit", "label": "Git auto-commit", "kind": "bool",
     "options": [False, True], "help": "commit the files hexiel changed after each finished turn"},
    {"key": "settings.notify", "label": "Notifications", "kind": "bool",
     "options": [True, False], "help": "desktop notification when approval is needed or a long turn ends"},
    {"key": "settings.search_backend", "label": "Web search", "kind": "choice",
     "options": ["duckduckgo", "searxng"], "help": "search engine for the web_search tool"},
    {"key": "settings.searxng_url", "label": "SearXNG URL", "kind": "text", "options": None,
     "help": "e.g. http://localhost:8888 (when web search = searxng)"},
]
FIELD_BY_KEY = {f["key"]: f for f in FIELDS}


# ---------------------------------------------------------------- reading

def _live_value(session, key: str):
    mc, st = session.model_config, session.settings
    if key == "profile":
        return mc.name
    if key == "model.reasoning":
        return (mc.extra_body or {}).get("reasoning_effort", "model default")
    if key.startswith("model."):
        return getattr(mc, key.split(".", 1)[1])
    if key == "settings.auto_approve":
        return _MODE_LABEL[config_mod.approve_mode(session.auto_approve, session.guard_commits)]
    return getattr(st, key.split(".", 1)[1])


_MODE_LABEL = {"off": "off", "except-commits": "except commits", "all": "everything"}


def _saved_value(cfg, key: str, profile: str):
    if key == "profile":
        return cfg.default_model
    if key == "settings.auto_approve":
        return _MODE_LABEL[config_mod.approve_mode(cfg.settings.auto_approve, cfg.settings.guard_commits)]
    mc = cfg.models.get(profile)
    if key == "model.reasoning":
        return (mc.extra_body or {}).get("reasoning_effort", "model default") if mc else None
    if key.startswith("model."):
        return getattr(mc, key.split(".", 1)[1]) if mc else None
    return getattr(cfg.settings, key.split(".", 1)[1])


def options_for(session, key: str) -> list | None:
    field = FIELD_BY_KEY[key]
    if key == "profile":
        return list(config_mod.load_config().models)
    if key in ("settings.planner_profile", "settings.executor_profile", "settings.compact_profile", "settings.worker_fallback_profile", "settings.cloud_fallback_profile", "settings.router_profile", "settings.overflow_profile"):
        return [""] + list(config_mod.load_config().models)
    if key in ("settings.fallback_profile", "settings.subagent_profile"):
        return [""] + [n for n in config_mod.load_config().models if n != session.model_config.name]
    if key == "model.model":
        mc = session.model_config
        found = config_mod.list_ollama_models(mc.base_url) if mc.base_url else []
        return sorted(set(found) | {mc.model})
    return field.get("options")


def rows(session, with_options: bool = False) -> list[dict]:
    """One row per editable field: live value, saved value, and whether the
    live value is a session-only change."""
    cfg = config_mod.load_config()
    out = []
    for f in FIELDS:
        live = _live_value(session, f["key"])
        saved = _saved_value(cfg, f["key"], session.model_config.name)
        row = {"key": f["key"], "label": f["label"], "kind": f["kind"], "help": f["help"],
               "value": live, "saved": saved, "session_only": live != saved}
        if with_options:
            row["options"] = options_for(session, f["key"])
        out.append(row)
    return out


# ---------------------------------------------------------------- coercion

def coerce(key: str, value):
    if key == "settings.auto_approve":
        return config_mod.parse_auto_approve(value)        # "off" | "except-commits" | "all"
    if key == "settings.workers":
        return int(value) if str(value).strip().isdigit() and int(value) >= 1 else "auto"
    kind = FIELD_BY_KEY[key]["kind"]
    if kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("true", "yes", "on", "1")
        return bool(value)
    if kind == "number":
        num = float(value)
        return int(num) if key != "settings.compact_threshold" else num
    return str(value).strip()


# ---------------------------------------------------------------- applying

def apply(session, key: str, value, scope: str) -> str:
    """Change `key` to `value`. scope: "session" (this run only) or "permanent"
    (also written to config.toml). Returns a human-readable confirmation."""
    if key not in FIELD_BY_KEY:
        raise KeyError(f"unknown setting {key}")
    if scope not in ("session", "permanent"):
        raise ValueError("scope must be 'session' or 'permanent'")
    value = coerce(key, value)
    profile = session.model_config.name

    # 1. live session
    if key == "profile":
        session.model_config = config_mod.load_config().model(value)
    elif key == "model.reasoning":
        extra = dict(session.model_config.extra_body or {})
        if value == "model default":
            extra.pop("reasoning_effort", None)
        else:
            extra["reasoning_effort"] = value
        session.model_config = dataclasses.replace(session.model_config, extra_body=extra)
    elif key.startswith("model."):
        session.model_config = dataclasses.replace(session.model_config,
                                                   **{key.split(".", 1)[1]: value})
    elif key == "settings.auto_approve":
        session.auto_approve = value != "off"
        session.guard_commits = value == "except-commits"
        session.settings.auto_approve, session.settings.guard_commits = session.auto_approve, session.guard_commits
    else:
        setattr(session.settings, key.split(".", 1)[1], value)

    # 2. config file
    label = FIELD_BY_KEY[key]["label"]
    shown = _MODE_LABEL[value] if key == "settings.auto_approve" else value
    if scope == "session":
        return f"{label} → {shown} (this session only)"
    if key == "profile":
        _write(None, "default", value)
    elif key == "model.reasoning":
        extra = dict(session.model_config.extra_body or {})
        _write(f"models.{profile}", "extra_body", extra if extra else None)
    elif key.startswith("model."):
        _write(f"models.{profile}", key.split(".", 1)[1], value)
    elif key == "settings.auto_approve":
        _write("settings", "auto_approve", {"off": False, "except-commits": "except-commits", "all": True}[value])
    else:
        _write("settings", key.split(".", 1)[1], value)
    return f"{label} → {shown} (saved to {config_mod.CONFIG_FILE})"


def add_profile(name: str, fields: dict) -> str:
    """Append a [models.<name>] profile to config.toml (refuses to overwrite)."""
    if name in config_mod.load_config().models:
        raise ValueError(f"a profile named '{name}' already exists")
    for key, value in fields.items():
        _write(f"models.{name}", key, value)
    return f"added profile '{name}' to {config_mod.CONFIG_FILE}"


# ---------------------------------------------------------------- TOML line editing

def toml_literal(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {toml_literal(v)}" for k, v in value.items()) + " }"
    return json.dumps(str(value))   # TOML basic string == JSON string for plain text


_HEADER = re.compile(r"^\s*\[([^\[\]]+)\]\s*(#.*)?$")


def _split_comment(rest: str) -> tuple[str, str]:
    """Split `value  # comment` respecting quotes and inline tables."""
    quote, depth = None, 0
    for i, ch in enumerate(rest):
        if quote:
            if ch == quote and rest[i - 1] != "\\":
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        elif ch == "#" and depth == 0:
            return rest[:i], rest[i:]
    return rest, ""


def set_toml_value(text: str, section: str | None, key: str, value) -> str:
    """Return `text` with `key` in `[section]` (None = top level) set to `value`;
    value None removes the key. Only the one affected line changes."""
    lines = text.splitlines(keepends=True)
    start, end = 0, len(lines)            # line range of the target section body
    if section is not None:
        start = None
        for i, line in enumerate(lines):
            m = _HEADER.match(line)
            if m and m.group(1).strip().replace('"', "") == section:
                start = i + 1
                break
        if start is None:                 # section missing: append it
            if value is None:
                return text
            sep = "" if text.endswith("\n") or not text else "\n"
            return text + f"{sep}\n[{section}]\n{key} = {toml_literal(value)}\n"
    for i in range(start, len(lines)):
        if _HEADER.match(lines[i]):
            end = i
            break
    key_re = re.compile(rf"^(\s*){re.escape(key)}(\s*=\s*)(.*?)(\r?\n)?$")
    for i in range(start, end):
        m = key_re.match(lines[i])
        if m:
            if value is None:
                del lines[i]
                return "".join(lines)
            _, comment = _split_comment(m.group(3))
            tail = f"   {comment.strip()}" if comment.strip() else ""
            lines[i] = f"{m.group(1)}{key}{m.group(2)}{toml_literal(value)}{tail}{m.group(4) or chr(10)}"
            return "".join(lines)
    if value is None:
        return text
    # key missing: insert after the section's last non-blank line
    insert_at = end
    while insert_at > start and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, f"{key} = {toml_literal(value)}\n")
    return "".join(lines)


def _write(section: str | None, key: str, value) -> None:
    import tomllib
    path = config_mod.CONFIG_FILE
    old = path.read_text() if path.exists() else ""
    new = set_toml_value(old, section, key, value)
    # verify before touching the file: must parse and read back the new value
    parsed = tomllib.loads(new)
    node = parsed
    for part in (section.split(".") if section else []):
        node = node.get(part, {})
    if value is None:
        assert key not in node, f"failed to remove {key}"
    else:
        assert node.get(key) == value, f"verify failed for {key}: {node.get(key)!r} != {value!r}"
    backup = path.with_name(path.name + ".bak")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)   # one-time safety copy of the hand-written original
    path.write_text(new)
