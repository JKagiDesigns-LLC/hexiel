"""hexiel's own key store: ~/.config/hexiel/auth.toml (mode 0600), the file /setup-gitlab already writes.

    [keys]
    ANTHROPIC_API_KEY = "sk-ant-…"      # exported into the environment at startup when the shell has no value  # pragma: allowlist secret
    OLLAMA_API_KEY = "…"

Profiles keep referring to keys by environment-variable name (api_key_env), so nothing else changes:
a key found by the first-run discovery, pasted with `hexiel auth set NAME`, or exported in your shell
all reach the provider the same way. The shell always wins over the file.

    hexiel auth set anthropic            # prompts for the key of profile 'anthropic' (hidden input)
    hexiel auth set OPENAI_API_KEY …     # or name the variable directly
    hexiel auth list                     # what is stored, masked
    hexiel auth remove ANTHROPIC_API_KEY
"""

from __future__ import annotations

import getpass
import os
import sys
import tomllib
from pathlib import Path

from .config import home_config_dir


def auth_path() -> Path:
    return home_config_dir() / "auth.toml"


def _read() -> dict:
    try:
        return tomllib.loads(auth_path().read_text())
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> Path:
    path = auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "# hexiel credentials (0600) — keys are exported to the environment at startup; the shell wins\n"
    for section, values in data.items():
        if not isinstance(values, dict):
            continue
        text += f"\n[{section}]\n" + "".join(f"{k} = {_literal(v)}\n" for k, v in values.items())
    path.write_text(text)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _literal(value) -> str:
    import json
    return json.dumps(str(value))


def mask(secret: str) -> str:
    secret = secret or ""
    return f"{secret[:7]}…{secret[-3:]}" if len(secret) > 12 else ("set" if secret else "")


def keys() -> dict[str, str]:
    """{ENV_NAME: value} stored in auth.toml."""
    return {str(k): str(v) for k, v in (_read().get("keys") or {}).items() if v}


def save_key(env: str, value: str) -> Path:
    data = _read()
    data.setdefault("keys", {})[env] = value.strip()
    return _write(data)


def remove_key(env: str) -> bool:
    data = _read()
    had = env in (data.get("keys") or {})
    if had:
        del data["keys"][env]
        _write(data)
    return had


def keys_into_env() -> list[str]:
    """Export stored keys the shell does not already set. Called at startup; returns the names exported."""
    out = []
    for env, value in keys().items():
        if not os.environ.get(env):
            os.environ[env] = value
            out.append(env)
    return out


def env_for_profile(name: str) -> str:
    """The api_key_env of a profile, or the name itself when it already looks like a variable."""
    if name.isupper() and "_" in name:
        return name
    from .config import load_config
    try:
        mc = load_config().models.get(name)
    except SystemExit:
        mc = None
    if mc is None:
        raise SystemExit(f"no profile named '{name}' — name the variable instead (e.g. ANTHROPIC_API_KEY)")
    if not mc.api_key_env:
        raise SystemExit(f"profile '{name}' needs no key (it talks to {mc.base_url or mc.provider})")
    return mc.api_key_env


def cli(argv: list[str]) -> int:
    """hexiel auth set NAME [VALUE] | list | remove NAME"""
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "list":
        stored = keys()
        if not stored:
            print(f"no keys stored in {auth_path()} (hexiel auth set NAME)")
            return 0
        for env, value in stored.items():
            shell = " (the shell sets its own value, which wins)" if os.environ.get(env) and os.environ[env] != value else ""
            print(f"  {env} = {mask(value)}{shell}")
        return 0
    if cmd == "set":
        if not rest:
            print("usage: hexiel auth set PROFILE-OR-VARIABLE [VALUE]")
            return 2
        env = env_for_profile(rest[0])
        value = rest[1] if len(rest) > 1 else (getpass.getpass(f"{env}: ") if sys.stdin.isatty() else sys.stdin.readline())
        value = value.strip()
        if not value:
            print("nothing stored (empty value)")
            return 2
        path = save_key(env, value)
        os.environ[env] = value
        print(f"stored {env} = {mask(value)} in {path} (0600)")
        return 0
    if cmd == "remove":
        if not rest:
            print("usage: hexiel auth remove VARIABLE")
            return 2
        print(f"removed {rest[0]}" if remove_key(rest[0]) else f"{rest[0]} was not stored")
        return 0
    print(f"unknown: hexiel auth {cmd} — try: set, list, remove")
    return 2
