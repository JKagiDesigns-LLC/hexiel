"""View and edit the system prompt — for this session, this directory, or globally.

`/prompt` (terminal and browser) shows the prompt the model currently works from
and lets the user edit it. Saved edits live in one of three places, in this
precedence:

1. this session  — kept in the saved chat, so resuming it keeps the edit
2. this directory — `<project>/.hexiel/system-prompt.md` (share it with the repo)
3. globally       — `~/.config/hexiel/system-prompt.md` (every session, anywhere)

No file edited → hexiel builds the default prompt (prompt.build_system_prompt).
"""

from __future__ import annotations

from pathlib import Path

FILENAME = "system-prompt.md"

SCOPE_LABEL = {"session": "this session", "directory": "this directory", "global": "global"}


def global_path() -> Path:
    from .config import home_config_dir
    return home_config_dir() / FILENAME


def dir_path(cwd: str) -> Path:
    return Path(cwd) / ".hexiel" / FILENAME


# ---------------------------------------------------------------- reading

def _read_file(path: Path) -> str | None:
    try:
        return path.read_text() if path.is_file() else None
    except OSError:
        return None


def source_of(session) -> str | None:
    """Where the current prompt comes from: "session", "directory", "global"
    or None for the default build."""
    if getattr(session, "prompt_override", None):
        return session.prompt_override
    for scope, path in (("directory", dir_path(session.cwd)), ("global", global_path())):
        if path.is_file():
            return scope
    return None


def default_prompt(session) -> str:
    """What build_system_prompt would produce right now (used on/for reset)."""
    text = getattr(session, "base_prompt", None)
    if text:
        return text
    from .skills import discover as discover_skills, prompt_section
    from .tooling import detect_toolchain, toolchain_section
    from . import prompt as prompt_mod
    return prompt_mod.build_system_prompt(session.cwd, prompt_section(discover_skills(session.cwd)),
                                          toolchain_section(detect_toolchain(session.cwd), session.cwd))


def get(session) -> dict:
    """What the /prompt modal shows: the current text and where it comes from."""
    return {"source": source_of(session) or "default", "text": session.system_prompt,
            "default": default_prompt(session)}


# ---------------------------------------------------------------- changing

def save(session, text: str, scope: str) -> str:
    """Store the edited prompt in `scope` ("session" | "directory" | "global")
    and make it live right away (the model sees it on its next step)."""
    path = {"directory": lambda: dir_path(session.cwd), "global": lambda: global_path()}.get(scope)
    if scope != "session" and path is None:
        raise ValueError("scope must be 'session', 'directory' or 'global'")
    if not str(text).strip():
        raise ValueError("the system prompt can't be empty — use Reset to go back to the default")
    if path is not None:
        try:
            path().parent.mkdir(parents=True, exist_ok=True)
            path().write_text(str(text).strip() + "\n")
        except OSError as exc:
            raise ValueError(f"could not write {path()}: {exc}")
    session.system_prompt = str(text).strip()
    session.prompt_override = scope
    session.save()      # kept with the saved chat (save() only stores it when prompt_override is set)
    where = {"session": "for this session only", "directory": f"in {dir_path(session.cwd)}",
             "global": f"in {global_path()}"}[scope]
    return f"system prompt saved {where} — the model sees it from its next step"


def reset(session) -> str:
    """Drop every override (session, directory, global) and rebuild the default prompt."""
    removed: list[str] = []
    if getattr(session, "prompt_override", None) == "session":
        removed.append("this session's edited copy")
    files = (("directory", dir_path(session.cwd)), ("global", global_path()))
    for scope, path in files:
        try:
            if path.is_file():
                path.unlink()
                removed.append(str(path))
        except OSError:
            pass
    session.prompt_override = None
    session.system_prompt = default_prompt(session)
    session.save()
    if removed:
        return "back to the default prompt (removed: " + "; ".join(removed) + ")"
    return "already on the default prompt"


def startup(session, resumed: dict | None) -> None:
    """Called once by build_session: restore a session-scoped edit from the saved
    chat, else apply whatever this directory / the global override file says.
    Also remembers the default prompt so Reset needs no rebuild."""
    session.prompt_override = None
    session.base_prompt = session.system_prompt
    text, source = None, "session"
    if resumed:
        saved = resumed.get("system_prompt") or ""
        if isinstance(saved, str) and saved.strip():
            text, source = saved, "session"
            if getattr(session.settings, "redact_secrets", "full") != "off":
                from . import redact
                text = redact.scrub(text)      # tape over a secret typed before redaction existed
    if text is None:
        for scope, path in (("directory", dir_path(session.cwd)), ("global", global_path())):
            found = _read_file(path)
            if found and found.strip():
                text, source = found.strip(), scope
                break
    if text and source != "default":
        session.system_prompt = text
        session.prompt_override = source


# ---------------------------------------------------------------- session payload (session.py calls back)

def save_extra(session) -> dict:
    """Extra keys for Session.save() when this chat runs an edited prompt."""
    if getattr(session, "prompt_override", None):
        return {"prompt_override": session.prompt_override,
                "system_prompt": session.system_prompt}
    return {}
