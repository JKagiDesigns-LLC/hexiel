"""hexiel doctor: preflight checks in the style of Neovim's :checkhealth — grouped, one status per line,
and under every line that is not OK the reason and the generic fix (install this, run that, set this).

    hexiel --doctor            the report
    hexiel --doctor --fix      also apply the safe generic fixes (create dirs, install optional extras, pull a model)
    hexiel --doctor --json     the same as data (CI, the web UI's Health page)
    /doctor                    the report inside a session

Levels: OK (works), INFO (optional, nothing to do), WARNING (works, but something is missing or odd),
ERROR (hexiel cannot do its job until this is fixed). A fresh install shows no ERROR unless the default
model cannot answer. The first-run wizard (`hexiel --setup`) reuses these checks and their advice."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import __version__
from . import sandbox as sandbox_
from .config import CONFIG_FILE, load_config, home_data_dir
from .gitlab import load_token

OK, INFO, WARNING, ERROR = "ok", "info", "warning", "error"
GROUPS = ("Runtime", "Models", "Tools", "Integrations", "Project")
_WORD = {OK: "OK", INFO: "INFO", WARNING: "WARNING", ERROR: "ERROR"}
_COLOUR = {OK: "\033[32m", INFO: "\033[2m", WARNING: "\033[33m", ERROR: "\033[31m"}


@dataclass
class Check:
    group: str
    name: str                      # stable key (run_checks' dict, tests, --json)
    level: str
    label: str
    detail: str = ""
    advice: tuple = ()             # lines of generic advice: a command to run, a thing to install, a link
    fix: Callable | None = None    # a safe generic fix `hexiel --doctor --fix` may apply (returns a message)
    fix_label: str = ""
    data: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.level in (OK, INFO)

    def as_dict(self) -> dict:
        return {"group": self.group, "name": self.name, "level": self.level, "label": self.label,
                "detail": self.detail, "advice": list(self.advice), "fixable": bool(self.fix),
                "fix": self.fix_label, **({"data": self.data} if self.data else {})}


def _mask(secret: str) -> str:
    return f"{secret[:7]}…{secret[-3:]}" if len(secret) > 12 else "set"


def _pkg_manager_install(package: str) -> str:
    """The install command for this OS's package manager, best effort (the doctor never guesses silently)."""
    if sys.platform == "darwin":
        return f"brew install {package}"
    if os.name == "nt":
        return f"winget install {package}"
    for pm, cmd in (("pacman", f"sudo pacman -S {package}"), ("apt", f"sudo apt install {package}"),
                    ("dnf", f"sudo dnf install {package}"), ("zypper", f"sudo zypper install {package}"),
                    ("apk", f"sudo apk add {package}")):
        if shutil.which(pm):
            return cmd
    return f"install {package} with your package manager"


def _pip_install(spec: str) -> str:
    """How to add a Python package to the environment hexiel runs in."""
    exe = Path(sys.executable)
    if "pipx" in str(exe):
        return f"pipx inject hexiel {spec}"
    return f"{exe} -m pip install {spec}"


# ---------------------------------------------------------------- the checks

def _runtime_checks(cfg) -> list[Check]:
    out = []
    v = sys.version_info
    out.append(Check("Runtime", "python", OK if v >= (3, 11) else WARNING, f"python {v.major}.{v.minor}.{v.micro}",
                     sys.executable, () if v >= (3, 11) else ("hexiel is tested on Python 3.11+; older versions may break",)))
    for mod, needs, spec, level in (("textual", "the terminal UI", "textual", ERROR), ("httpx", "every provider", "httpx", ERROR),
                                    ("aiohttp", "the browser UI (hexiel --serve)", "'hexiel[serve]'", WARNING),
                                    ("textual_image", "inline image thumbnails in the terminal", "'hexiel[view]'", INFO),
                                    ("tree_sitter_language_pack", "exact symbol ranges in non-Python code (find, where_is)",
                                     "'hexiel[code]'", INFO)):
        try:
            __import__(mod)
            out.append(Check("Runtime", f"dep:{mod}", OK, mod, f"for {needs}"))
        except ImportError:
            spec_cmd = _pip_install(spec)
            out.append(Check("Runtime", f"dep:{mod}", level, f"{mod} not installed", f"needed for {needs}",
                             (spec_cmd,), fix=(lambda s=spec: _pip_fix(s)) if level != ERROR else None,
                             fix_label=spec_cmd))
    mode = str(getattr(cfg.settings, "sandbox", "off") or "off").lower() if cfg else "off"
    infra = sandbox_.available()
    have = ", ".join(k for k, v in infra.items() if v)
    if mode == "safe" and sys.platform != "darwin" and not infra.get("bubblewrap"):
        out.append(Check("Runtime", "sandbox", ERROR, "sandbox = safe but bubblewrap is missing", "", (sandbox_.BWRAP_HINT,)))
    elif mode == "safe" and sys.platform == "darwin" and not infra.get("sandbox-exec"):
        out.append(Check("Runtime", "sandbox", ERROR, "sandbox = safe but sandbox-exec is missing", "macOS ships it at /usr/bin"))
    elif mode == "container" and not (infra.get("docker") or infra.get("podman")):
        out.append(Check("Runtime", "sandbox", ERROR, "sandbox = container but neither docker nor podman is installed", "",
                         (_pkg_manager_install("docker"), "then set sandbox_image in config.toml")))
    else:
        out.append(Check("Runtime", "sandbox", OK if mode != "off" else INFO, f"sandbox {mode}",
                         have or "no sandbox tooling installed (fine while it is off)"))
    if sys.stdout.isatty():
        term = os.environ.get("COLORTERM", "")
        out.append(Check("Runtime", "terminal", OK if term in ("truecolor", "24bit") else INFO,
                         "terminal colours", f"{os.environ.get('TERM', '?')}, COLORTERM={term or 'unset'}",
                         () if term in ("truecolor", "24bit") else ("most terminals support 24-bit colour; set COLORTERM=truecolor if yours does",)))
    exe = shutil.which("hexiel")
    if not exe:
        out.append(Check("Runtime", "path", WARNING, "hexiel is not on PATH", "a new terminal will not find the command",
                         ("pipx ensurepath   (then open a new terminal)", "or add ~/.local/bin to your PATH")))
    else:
        running = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
        shadow = running and running.name == "hexiel" and Path(exe).resolve() != running
        out.append(Check("Runtime", "path", WARNING if shadow else OK, "hexiel on PATH",
                         exe + (f" — this run came from {running}" if shadow else ""),
                         ("a different hexiel is first on PATH: `hexiel` in a new terminal runs that one — fine if it is "
                          "the same version; otherwise remove the old install (pipx uninstall hexiel / pip uninstall hexiel) "
                          "or point ~/.local/bin/hexiel at this one",)
                         if shadow else ()))
    try:
        usage = shutil.disk_usage(home_data_dir())
        free_gb = usage.free / 1e9
        out.append(Check("Runtime", "disk", WARNING if free_gb < 5 else INFO, "free disk space",
                         f"{free_gb:.0f} GB free at {home_data_dir()}",
                         ("local models need 2–20 GB each; free some space before pulling one",) if free_gb < 5 else ()))
    except OSError:
        pass
    return out


def _pip_fix(spec: str) -> str:
    cmd = [sys.executable, "-m", "pip", "install", spec.strip("'")]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)   # nosec B603 - fixed argv
    return f"installed {spec}" if proc.returncode == 0 else f"pip failed: {(proc.stderr or proc.stdout)[-300:]}"


def _model_checks(cfg, cfg_error: str, existed: bool = True) -> list[Check]:
    out = []
    if not existed:
        out.append(Check("Models", "config", INFO, "no config.toml yet", "hexiel writes a starter config the first time it runs",
                         (f"or run `hexiel --setup` to pick a model and keys now ({CONFIG_FILE})",)))
    else:
        out.append(Check("Models", "config", OK, "config.toml", str(CONFIG_FILE)))
    if cfg is None:
        out.append(Check("Models", "config_parse", ERROR, "config.toml does not parse", cfg_error,
                         (f"fix the line it names in {CONFIG_FILE}, or move the file aside and run hexiel to get a fresh one",)))
        return out
    out.append(Check("Models", "config_parse", OK, "config parses", f"{len(cfg.models)} profiles: " + ", ".join(cfg.models)))
    name = cfg.default_model
    mc = cfg.models.get(name)
    if mc is None:
        out.append(Check("Models", "model", ERROR, f"default profile '{name}' does not exist",
                         "available: " + ", ".join(cfg.models), (f"set default = \"{next(iter(cfg.models), '')}\" in config.toml",)))
        return out
    out.append(Check("Models", "model", OK, f"default profile '{name}'", f"{mc.model} @ {mc.base_url or mc.provider}"))
    out += _endpoint_checks(mc)
    by_env: dict[str, list[str]] = {}
    for pname, p in cfg.models.items():
        env = getattr(p, "api_key_env", "") or ""
        if env and pname != name and env != (getattr(mc, "api_key_env", "") or ""):
            by_env.setdefault(env, []).append(pname)
    for env, names in by_env.items():
        key = os.environ.get(env, "")
        out.append(Check("Models", f"key:{env}", OK if key else INFO, f"{env} for profile(s) {', '.join(names)}",
                         _mask(key) if key else "not set (fine if you do not use them)",
                         () if key else (f"export {env}=…   (or: hexiel auth set {names[0]})",)))
    return out


def _endpoint_checks(mc) -> list[Check]:
    """Can the default model answer right now? One cheap request proves it; the failure says what to do."""
    out = []
    env = getattr(mc, "api_key_env", "") or ""
    key = os.environ.get(env, "") if env else ""
    host = (mc.base_url or "").lower()
    cloud_model = mc.model.endswith(":cloud") or mc.model.endswith("-cloud")
    if env and not key and (not mc.base_url or "ollama.com" in host or "localhost" not in host and "127.0.0.1" not in host):
        out.append(Check("Models", "provider", ERROR, f"{mc.provider} key {env} is not set", "the default model cannot answer without it",
                         (f"export {env}=…   (put it in your shell profile to keep it)",
                          *(("https://ollama.com/settings/keys",) if "ollama" in host or mc.provider == "ollama" else ()))))
        return out
    if not mc.base_url:
        out.append(Check("Models", "provider", OK, f"{mc.provider} key {env}", _mask(key)))
        return out
    from .config import list_ollama_models
    models = list_ollama_models(mc.base_url)
    if not models:
        local = any(h in host for h in ("localhost", "127.0.0.1"))
        out.append(Check("Models", "provider", ERROR, f"model server not reachable at {mc.base_url}", "",
                         ("start it: ollama serve   (or open the Ollama app)" if local else "check the URL and your network",
                          *((_pkg_manager_install("ollama") + "   if Ollama is not installed: https://ollama.com/download",) if local else ()))))
        return out
    wanted = mc.model in models
    if not wanted and not cloud_model:
        out.append(Check("Models", "provider", ERROR, f"'{mc.model}' is not on the server", f"{len(models)} models there",
                         (f"ollama pull {mc.model}", "or pick one that is there: /model"),
                         fix=lambda: _pull_fix(mc), fix_label=f"ollama pull {mc.model}"))
        return out
    out.append(Check("Models", "provider", OK, "model server", f"{mc.base_url} — {len(models)} models"
                     + ("" if wanted else f"; '{mc.model}' runs on Ollama Cloud")))
    verdict = probe_model(mc, key)
    if verdict == "ok":
        out.append(Check("Models", "answers", OK, f"'{mc.model}' answers", "one-token test request succeeded"))
    elif verdict == "unauthorized":
        out.append(Check("Models", "answers", ERROR, f"'{mc.model}' needs an Ollama Cloud sign-in",
                         "the server returned 401 Unauthorized",
                         ("run: ollama signin   (creates or links your ollama.com account)",
                          "or set OLLAMA_API_KEY from https://ollama.com/settings/keys")))
    elif verdict:
        out.append(Check("Models", "answers", WARNING, f"'{mc.model}' did not answer the test request", verdict,
                         ("the first request loads the model (slow once); run `hexiel -p hi` to see the full error",)))
    if ":11434" in host:
        for ok, label, detail, fix in ollama_runtime_checks(mc):
            out.append(Check("Models", f"runtime:{label}", OK if ok else WARNING, label, detail, (fix,) if fix else ()))
    return out


def probe_model(mc, key: str = "") -> str:
    """'ok', 'unauthorized', '' (no probe possible) or the error text — a one-token chat request."""
    import httpx
    url = (mc.base_url or "").rstrip("/")
    try:
        if getattr(mc, "provider", "") == "anthropic" and not url:
            if not key:
                return ""
            resp = httpx.post("https://api.anthropic.com/v1/messages", timeout=60,
                              headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                              json={"model": mc.model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]})
        elif not url:
            return ""
        else:
            headers = {"authorization": f"Bearer {key}"} if key else {}
            resp = httpx.post(f"{url}/chat/completions", headers=headers, timeout=60,
                              json={"model": mc.model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1})
    except httpx.HTTPError as exc:
        return f"{type(exc).__name__}: {exc}"[:160]
    if resp.status_code == 200:
        return "ok"
    if resp.status_code == 401:
        return "unauthorized"
    return f"HTTP {resp.status_code}: {resp.text[:160]}"


def _pull_fix(mc) -> str:
    proc = subprocess.run(["ollama", "pull", mc.model], capture_output=True, text=True, timeout=3600)   # nosec B603 B607
    return f"pulled {mc.model}" if proc.returncode == 0 else f"ollama pull failed: {(proc.stderr or proc.stdout)[-300:]}"


def ollama_runtime_checks(model_cfg) -> list[tuple[bool, str, str, str]]:
    """What the Ollama server is actually doing with the configured model."""
    import httpx
    native = model_cfg.base_url.rstrip("/")
    native = native[:-3] if native.endswith("/v1") else native
    try:
        loaded = httpx.get(f"{native}/api/ps", timeout=5).json().get("models", [])
    except (httpx.HTTPError, ValueError):
        return []
    entry = next((m for m in loaded if m.get("name") == model_cfg.model or m.get("model") == model_cfg.model), None)
    if entry is None:
        return [(True, "model loaded on server", f"{model_cfg.model} not loaded yet — first request loads it "
                 "(slow once); set OLLAMA_KEEP_ALIVE=-1 on the server to keep it loaded", "")]
    checks = []
    ctx = entry.get("context_length") or 0
    if ctx:
        ok = model_cfg.context_window <= ctx
        checks.append((ok, "context window vs server", f"profile {model_cfg.context_window:,} / server {ctx:,} tokens",
                       "" if ok else f"set context_window = {ctx} for this profile (or raise "
                       f"OLLAMA_CONTEXT_LENGTH on the server) — otherwise long chats get silently truncated"))
    size, vram = entry.get("size") or 0, entry.get("size_vram") or 0
    if size:
        gpu = 100 * vram // size
        checks.append((gpu >= 95, "model on GPU", f"{gpu}% of {size / 1e9:.0f} GB in GPU memory",
                       "" if gpu >= 95 else "part of the model runs on the CPU (much slower): use a smaller "
                       "model/quantization or lower OLLAMA_CONTEXT_LENGTH"))
    return checks


def _tool_checks(cwd: str) -> list[Check]:
    from .tooling import detect_toolchain
    out = []
    chain = detect_toolchain(cwd)
    git = shutil.which("git")
    out.append(Check("Tools", "git", OK if git else WARNING, "git", git or "not installed — no /undo checkpoints, /ship, /heal, worktrees",
                     () if git else (_pkg_manager_install("git"),)))
    if chain.container:
        c = chain.container
        out.append(Check("Tools", "container", OK, "container runtime", f"{c.binary} {c.version}" + (" + compose" if c.compose_command else "")))
    else:
        out.append(Check("Tools", "container", INFO, "no container runtime", "the container tool is off (docker or podman)",
                         (_pkg_manager_install("docker"),)))
    langs = {t.name for t in chain.tools if t.category == "language"}
    out.append(Check("Tools", "languages", OK if langs else INFO, "language runtimes",
                     ", ".join(f"{t.name} {t.version}".strip() for t in chain.tools if t.category == "language") or "none on PATH"))
    if chain.test_runners:
        out.append(Check("Tools", "test_runner", OK, "test runner", ", ".join(f"{r.label}: {' '.join(r.command)}" for r in chain.test_runners)))
    else:
        out.append(Check("Tools", "test_runner", INFO, "no test runner detected in this project", "the test tool falls back to bash"))
    want = []
    if "python3" in langs or any((Path(cwd) / n).exists() for n in ("pyproject.toml", "requirements.txt")):
        want += [("pyright", "Python diagnostics after edits", "pipx install pyright"), ("ruff", "Python formatting", "pipx install ruff")]
    if (Path(cwd) / "package.json").exists():
        want += [("prettier", "JS/TS formatting", "npm i -g prettier"), ("tsc", "TypeScript diagnostics", "npm i -g typescript")]
    if (Path(cwd) / "go.mod").exists():
        want += [("gofmt", "Go formatting", _pkg_manager_install("go"))]
    if (Path(cwd) / "Cargo.toml").exists():
        want += [("rustfmt", "Rust formatting", "rustup component add rustfmt")]
    for name, why, how in want:
        found = shutil.which(name) or (shutil.which("npx") and name in ("prettier", "tsc"))
        out.append(Check("Tools", f"helper:{name}", OK if found else INFO, name, why if found else f"not found — {why} is off",
                         () if found else (how,)))
    for name, why, how in (("tesseract", "the ocr tool (text out of screenshots)", _pkg_manager_install("tesseract")),):
        found = shutil.which(name)
        out.append(Check("Tools", f"helper:{name}", OK if found else INFO, name, why if found else f"not found — {why} is off",
                         () if found else (how,)))
    pwsh = shutil.which("pwsh") or shutil.which("powershell.exe") or shutil.which("powershell")
    if os.name == "nt":
        out.append(Check("Tools", "powershell", OK if pwsh else ERROR, "PowerShell", pwsh or "missing — the shell tools need it",
                         () if pwsh else ("winget install Microsoft.PowerShell",)))
    else:
        out.append(Check("Tools", "powershell", OK if pwsh else INFO, "powershell", pwsh or "not installed (optional on this OS)"))
    return out


def _integration_checks() -> list[Check]:
    out = []
    token = load_token()
    out.append(Check("Integrations", "gitlab", OK if token else INFO, "GitLab token",
                     "set — /ship opens merge requests" if token else "not set — /ship and /heal push branches but cannot open merge requests",
                     () if token else ("create one (api scope): https://gitlab.com/-/user_settings/personal_access_tokens",
                                       "then in a session: /setup-gitlab <token>")))
    try:
        from . import remote
        signed = bool(remote.load_config().get("device_token"))
    except Exception:  # noqa: BLE001
        signed = False
    out.append(Check("Integrations", "remote", OK if signed else INFO, "Hexiel Remote",
                     "signed in" if signed else "not signed in (optional, paid: drive this machine from a browser or phone)",
                     () if signed else ("hexiel remote login",)))
    key = os.environ.get("OLLAMA_API_KEY", "")
    out.append(Check("Integrations", "ollama_key", OK if key else INFO, "Ollama Cloud key",
                     f"{_mask(key)} — the budget bar follows your plan" if key else "not set — Ollama Cloud spend is estimated, not read from your account",
                     () if key else ("https://ollama.com/settings/keys → export OLLAMA_API_KEY=…",)))
    return out


def _project_checks(cwd: str) -> list[Check]:
    out = []
    mem = home_data_dir() / "memory"
    out.append(Check("Project", "memory", OK if mem.is_dir() else INFO, "memory dir", str(mem) + ("" if mem.is_dir() else " (created on first run)"),
                     fix=None if mem.is_dir() else (lambda: (mem.mkdir(parents=True, exist_ok=True), f"created {mem}")[1]),
                     fix_label="" if mem.is_dir() else f"mkdir -p {mem}"))
    from .skills import discover
    skills = discover(cwd)
    out.append(Check("Project", "skills", OK if skills else INFO, "skills",
                     ", ".join(s.name for s in skills[:8]) + (" …" if len(skills) > 8 else "") if skills
                     else "none — ~/.claude/skills is read as-is if you have any (optional)"))
    try:
        from .instructions import discover as find_rules
        rules = find_rules(cwd)
        out.append(Check("Project", "instructions", OK if rules else INFO, "instruction files",
                         ", ".join(sorted({Path(getattr(r, 'path', str(r))).name for r in rules})[:6]) if rules
                         else "none (CLAUDE.md, AGENTS.md, .cursor/rules… are read when present)"))
    except Exception:  # noqa: BLE001
        pass
    try:
        from .knowledge import detected_topics, topics
        from .knowledge import _connect as kcon
        con = kcon(create=False)
        names = [t for t, _, _ in topics(con)] if con else []
        if con:
            con.close()
        if names:
            used = detected_topics(cwd)
            out.append(Check("Project", "knowledge", OK, "knowledge library", f"{len(names)} topic(s)"
                             + (f"; this project uses {', '.join(used)}" if used else "; none match this project")))
        else:
            out.append(Check("Project", "knowledge", INFO, "knowledge library empty", "optional: version-exact docs the model cites",
                             ("hexiel knowledge add <docs url> -t <topic>",)))
    except Exception:  # noqa: BLE001
        pass
    from .heal import unhealed
    incidents = unhealed(cwd)
    out.append(Check("Project", "incidents", WARNING if incidents else OK, "self-heal queue",
                     f"{len(incidents)} unhealed incident(s)" if incidents else "all clear",
                     ("run /heal in a session to reproduce and fix them",) if incidents else ()))
    return out


def checks(cwd: str, probe: bool = True) -> list[Check]:
    """Every check, grouped in GROUPS order. `probe=False` skips the network (tests, the wizard's quick pass)."""
    cfg, cfg_error = None, ""
    existed = CONFIG_FILE.exists()               # load_config() writes the starter file: remember what was there
    try:
        cfg = load_config()
    except SystemExit as exc:
        cfg_error = str(exc)
    out = _runtime_checks(cfg)
    if probe:
        out += _model_checks(cfg, cfg_error, existed)
    else:
        out += _model_checks_offline(cfg, cfg_error, existed)
    out += _tool_checks(cwd) + _integration_checks() + _project_checks(cwd)
    order = {g: i for i, g in enumerate(GROUPS)}
    out.sort(key=lambda c: order.get(c.group, 99))
    return out


def _model_checks_offline(cfg, cfg_error: str, existed: bool = True) -> list[Check]:
    saved = globals()["_endpoint_checks"]
    globals()["_endpoint_checks"] = lambda mc: []
    try:
        return _model_checks(cfg, cfg_error, existed)
    finally:
        globals()["_endpoint_checks"] = saved


# ---------------------------------------------------------------- output

def render(items: list[Check], colour: bool | None = None, title: bool = True) -> str:
    colour = sys.stdout.isatty() if colour is None else colour
    lines = []
    if title:
        head = f"hexiel {__version__} — doctor"
        lines += [head, "=" * len(head)]
    for group in GROUPS:
        rows = [c for c in items if c.group == group]
        if not rows:
            continue
        lines += ["", f"## {group}"]
        for c in rows:
            word = _WORD[c.level]
            tag = f"{_COLOUR[c.level]}{word}\033[0m" if colour else word
            lines.append(f"- {tag} {c.label}" + (f" — {c.detail}" if c.detail else ""))
            for i, a in enumerate(c.advice):
                lines.append(("  - ADVICE: " if i == 0 else "            ") + a)
    lines += ["", summary(items)]
    return "\n".join(lines)


def summary(items: list[Check]) -> str:
    n = {lvl: sum(1 for c in items if c.level == lvl) for lvl in (ERROR, WARNING, INFO)}
    fixable = sum(1 for c in items if c.fix and c.level != OK)
    if not n[ERROR] and not n[WARNING]:
        text = "all good — hexiel is ready"
    else:
        text = " · ".join(f"{n[l]} {_WORD[l].lower()}{'s' if n[l] != 1 and l != INFO else ''}" for l in (ERROR, WARNING) if n[l])
    if fixable:
        text += f" — run `hexiel --doctor --fix` to apply {fixable} generic fix(es)"
    return text


def apply_fixes(items: list[Check]) -> list[str]:
    done = []
    for c in items:
        if c.fix and c.level != OK:
            try:
                done.append(f"{c.label}: {c.fix()}")
            except Exception as exc:  # noqa: BLE001 - one failed fix must not stop the others
                done.append(f"{c.label}: fix failed — {exc}")
    return done


def as_json(items: list[Check]) -> str:
    return json.dumps({"version": __version__, "platform": f"{platform.system()} {platform.machine()}",
                       "checks": [c.as_dict() for c in items], "summary": summary(items),
                       "errors": sum(1 for c in items if c.level == ERROR)}, indent=1)


def run_checks(cwd: str) -> dict:
    """Print the report; return {name: ok} (ok = OK or INFO) — the shape older callers and tests expect."""
    items = checks(cwd)
    print(render(items))
    return {c.name: c.ok for c in items}


def doctor_main(cwd: str = ".", fix: bool = False, as_json_out: bool = False) -> int:
    items = checks(cwd)
    if as_json_out:
        print(as_json(items))
        return 1 if any(c.level == ERROR for c in items) else 0
    print(render(items))
    if fix:
        done = apply_fixes(items)
        if done:
            print("\nfixes applied:")
            print("\n".join(f"  - {d}" for d in done))
            items = checks(cwd)
            print("\n" + summary(items))
        else:
            print("\nnothing to fix automatically")
    return 1 if any(c.level == ERROR for c in items) else 0


# ---------------------------------------------------------------- first run

def wizard(cwd: str) -> None:
    """First-run guided setup (setup.py): discovery of keys and agents, review, a proven default model,
    GitLab token and Hexiel Remote notes. HEXIEL_SETUP_OFFLINE=1 skips every network probe."""
    from . import setup
    setup.run(cwd, network=os.environ.get("HEXIEL_SETUP_OFFLINE") != "1")
