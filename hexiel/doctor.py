"""hexiel doctor: preflight checks with copy-paste fixes, plus the first-run
wizard (same checks, guided). Run `hexiel doctor` any time."""

import os
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import CONFIG_FILE, load_config, home_data_dir
from .gitlab import load_token


def _check(label: str, ok: bool, detail: str = "", fix: str = "") -> bool:
    mark = "\033[32m✓\033[0m" if sys.stdout.isatty() else "✓" if ok else "✗"
    if not ok:
        mark = "\033[31m✗\033[0m" if sys.stdout.isatty() else "✗"
    line = f" {mark} {label}"
    if detail:
        line += f" — {detail}"
    print(line)
    if not ok and fix:
        print(f"     fix: {fix}")
    return ok


def run_checks(cwd: str) -> dict:
    """Run every check; returns {name: (ok, detail, fix)} and prints results."""
    sys.path.insert(0, str(Path(__file__).parent))
    results: dict[str, bool] = {}
    print(f"hexiel {__version__} — doctor\n")

    # config file
    ok = CONFIG_FILE.exists()
    results["config"] = _check(
        "configuration", ok, str(CONFIG_FILE) if ok else "no config.toml yet",
        fix="just run hexiel once — it writes a starter config")

    cfg = None
    try:
        cfg = load_config()
        results["config_parse"] = _check("config parses", True, ", ".join(cfg.models))
    except SystemExit as exc:
        results["config_parse"] = _check("config parses", False, str(exc))
        return results

    # python deps
    for mod, needs in (("textual", "the TUI"), ("httpx", "all providers"),
                       ("aiohttp", "the web UI (hexiel serve)")):
        try:
            __import__(mod)
            results[f"dep:{mod}"] = _check(f"python: {mod}", True, f"for {needs}")
        except ImportError:
            results[f"dep:{mod}"] = _check(
                f"python: {mod}", False, f"needed for {needs}",
                fix=f"pip install ({mod}) in your hexiel venv")

    # provider reachability
    default_name = cfg.default_model
    model_cfg = cfg.models.get(default_name)
    results["model"] = _check(
        f"default model '{default_name}'", model_cfg is not None,
        f"{model_cfg.model} @ {model_cfg.base_url or model_cfg.provider}" if model_cfg else "")
    if model_cfg and model_cfg.base_url:
        import httpx
        try:
            from .config import list_ollama_models
            models = list_ollama_models(model_cfg.base_url)
            reachable = bool(models)
            wanted = model_cfg.model in models
            results["provider"] = _check(
                "provider endpoint", reachable, f"{len(models)} models seen, "
                f"target '{model_cfg.model}' {'available' if wanted else 'NOT in inventory'}",
                fix="" if wanted else
                    f"ollama pull {model_cfg.model}  (or /model <another>)")
        except Exception as exc:  # noqa: BLE001
            results["provider"] = _check("provider endpoint", False, repr(exc),
                                         fix="start ollama serve (localhost:11434)")
    elif model_cfg and model_cfg.api_key_env:
        present = bool(os.environ.get(model_cfg.api_key_env))
        results["provider"] = _check(
            f"{model_cfg.provider} API key", present, model_cfg.api_key_env,
            fix=f"export {model_cfg.api_key_env}=sk-… (or put it in shell profile)")

    # toolchain
    from .tooling import detect_toolchain
    chain = detect_toolchain(cwd)
    container = chain.container.binary if chain.container else None
    results["container"] = _check(
        "container runtime (docker/podman)", bool(container),
        f"{container}{f' + compose' if (chain.container and chain.container.compose_command) else ''}"
        if container else "none found — container tool disabled",
        fix="sudo pacman -S docker && sudo systemctl enable --now docker")
    results["test_runner"] = _check(
        "test runner detection", bool(chain.test_runners),
        ", ".join(r.label for r in chain.test_runners) or "none in this project")
    results["git"] = _check("git", bool(shutil.which("git")),
                            "(needed for the /ship and /heal flows)",
                            fix="sudo pacman -S git")
    pwsh = shutil.which("pwsh") or shutil.which("powershell.exe") or shutil.which("powershell")
    if os.name == "nt":
        results["powershell"] = _check(
            "PowerShell", bool(pwsh), pwsh or "missing — the shell tools need it",
            fix="winget install Microsoft.PowerShell")
    else:
        results["powershell"] = _check(
            "powershell tool", bool(pwsh),
            pwsh or "not installed — optional here, required on Windows hosts",
            fix="yay -S powershell-bin (or winget install Microsoft.PowerShell)")

    # gitlab token
    token = load_token()
    results["gitlab"] = _check(
        "GitLab token", bool(token),
        "set (proposes MRs with /ship)" if token else "not set — /ship /heal can only push, not open MRs",
        fix="create one: https://gitlab.com/-/user_settings/personal_access_tokens (api scope),"
            " then HEXIEL_GITLAB_TOKEN=… hexiel or /setup-gitlab <token>")

    # memory + skills dirs
    mem = home_data_dir() / "memory"
    results["memory"] = _check("memory dir", mem.is_dir(), str(mem),
                               fix="created automatically on first run; check permissions")
    from .skills import discover
    skills = discover(cwd)
    results["skills"] = _check("skills", len(skills) > 0,
                               ", ".join(s.name for s in skills[:8]) +
                               (" …" if len(skills) > 8 else "") or "none installed")

    # incidents
    from .heal import unhealed
    incidents = unhealed(cwd)
    results["incidents"] = _check("self-heal queue", not incidents,
                                  "all clear" if not incidents
                                  else f"{len(incidents)} unhealed — run /heal in a session")

    return results


def wizard(cwd: str) -> None:
    """First-run guided setup: config, provider pick, gitlab token (optional)."""
    print("Welcome to hexiel — guided setup (all questions optional, Enter keeps defaults)\n")
    if not CONFIG_FILE.exists():
        load_config()   # writes starter config
        print(f"  wrote {CONFIG_FILE}")
    from .config import list_ollama_models
    models = list_ollama_models()
    if models:
        names = ", ".join(models[:12]) + (" …" if len(models) > 12 else "")
        print(f"  Ollama reachable — models: {names}")
        print("  config already defaults to ollama cloud models; /model NAME switches anytime")
    else:
        print("  Ollama not reachable on localhost:11434 — `ollama serve` + "
              "`ollama pull <model>` enables it, or add a cloud profile to config.toml")
    if not load_token():
        print("  no GitLab token: /ship and /heal can push branches but will not open MRs.")
        print("  add later with /setup-gitlab <token> (api scope PAT).")
    print("\nDone. Start a session with `hexiel` (TUI), `hexiel --serve` (browser), or `hexiel -p \"…\"`.")


def doctor_main(cwd: str = ".") -> int:
    results = run_checks(cwd)
    failed = [k for k, ok in results.items() if not ok]
    print()
    if not failed:
        print("all checks passed — hexiel is ready")
        return 0
    print(f"{len(failed)} check(s) need attention: {', '.join(failed)}")
    return 1