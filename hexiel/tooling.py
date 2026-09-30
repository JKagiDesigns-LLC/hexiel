"""Native toolchain discovery: installed SDKs/CLIs, test runners, container runtimes.

The detected toolchain is injected into the system prompt so the agent knows
exactly what it can call natively, and the `test` and `container` tools use
these bindings directly instead of the agent having to guess commands.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# name -> (category, version command, version arg index note)
TOOL_SPECS: dict[str, tuple[str, list[str], str]] = {
    # version control
    "git": ("vcs", ["git", "--version"], "last"),
    "gh": ("vcs", ["gh", "--version"], "first"),
    # languages
    "python3": ("language", ["python3", "--version"], "last"),
    "node": ("language", ["node", "--version"], "last"),
    "bun": ("language", ["bun", "--version"], "last"),
    "deno": ("language", ["deno", "--version"], "first"),
    "go": ("language", ["go", "version"], "last"),
    "rustc": ("language", ["rustc", "--version"], ""),
    "cargo": ("language", ["cargo", "--version"], "last"),
    "java": ("language", ["java", "-version"], "any"),
    "dotnet": ("language", ["dotnet", "--version"], "last"),
    "ruby": ("language", ["ruby", "--version"], "last"),
    "php": ("language", ["php", "--version"], "first"),
    "zig": ("language", ["zig", "version"], "last"),
    # package managers
    "pip": ("package", ["pip", "--version"], "last"),
    "uv": ("package", ["uv", "--version"], "last"),
    "npm": ("package", ["npm", "--version"], "last"),
    "pnpm": ("package", ["pnpm", "--version"], "last"),
    "yarn": ("package", ["yarn", "--version"], "first"),
    "bundler": ("package", ["bundle", "--version"], "last"),
    # test runners
    "pytest": ("test", ["pytest", "--version"], "last"),
    "cargo-test": ("test", ["cargo", "--version"], "last"),
    "go-test": ("test", ["go", "version"], "last"),
    # containers
    "docker": ("container", ["docker", "--version"], "last"),
    "podman": ("container", ["podman", "--version"], "last"),
    # other utility
    "make": ("build", ["make", "--version"], "first"),
    "cmake": ("build", ["cmake", "--version"], "last"),
    "magick": ("images", ["magick", "--version"], "any"),
    "ffmpeg": ("media", ["ffmpeg", "-version"], "first"),
    "kubectl": ("cloud", ["kubectl", "version", "--client"], "first"),
    "helm": ("cloud", ["helm", "version"], "first"),
    "terraform": ("cloud", ["terraform", "-version"], "last"),
    "opencode": ("agents", ["opencode", "--version"], "last"),
    "claude": ("agents", ["claude", "--version"], "last"),
}


@dataclass
class Tool:
    name: str
    category: str
    version: str
    path: str


@dataclass
class TestRunner:
    label: str
    command: list[str]
    note: str = ""


@dataclass
class ContainerRuntime:
    binary: str            # "docker" or "podman"
    version: str = ""
    compose_command: tuple | None = None   # e.g. ["docker", "compose"], if the plugin exists


@dataclass
class Toolchain:
    tools: list[Tool] = field(default_factory=list)
    test_runners: list[TestRunner] = field(default_factory=list)
    container: ContainerRuntime | None = None


def _version(cmd: list[str], path: str) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        out = (proc.stdout or proc.stderr).strip().splitlines()
        return " ".join(out[0].split()[-3:])[:60] if out else ""
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return ""


def _which(name: str) -> str | None:
    return shutil.which(name)


def _detect_container() -> ContainerRuntime | None:
    for binary in ("docker", "podman"):   # docker first unless absent; user can flip HEXIEL_CONTAINER
        if forced := os.environ.get("HEXIEL_CONTAINER"):
            if binary != forced:
                continue
        if _which(binary):
            runtime = ContainerRuntime(binary=binary)
            try:
                proc = subprocess.run([binary, "--version"], capture_output=True,
                                      text=True, timeout=8)
                runtime.version = proc.stdout.strip()[:60]
                compose = subprocess.run([binary, "compose", "version"], capture_output=True,
                                         text=True, timeout=10)
                if compose.returncode == 0:
                    runtime.compose_command = [binary, "compose"]
            except (subprocess.SubprocessError, FileNotFoundError, OSError):
                pass
            return runtime
    if forced := os.environ.get("HEXIEL_CONTAINER"):
        # maybe only the forced one exists; try once more without docker-first ordering
        if _which(forced):
            return ContainerRuntime(binary=forced)
    return None


def _venv_bin(cwd: str, name: str) -> str | None:
    """Find a runner binary in the project's virtualenv even if it's not on PATH."""
    for candidate in (Path(cwd) / ".venv" / "bin" / name,
                      Path(cwd) / "venv" / "bin" / name,
                      Path(cwd) / "node_modules" / ".bin" / name):
        if candidate.is_file():
            return str(candidate)
    return None


def _detect_test_runners(cwd: str) -> list[TestRunner]:
    root = Path(cwd)
    runners: list[TestRunner] = []

    pytest_bin = _which("pytest") or _venv_bin(cwd, "pytest")
    if pytest_bin and any(
        (root / marker).exists()
        for marker in ("pytest.ini", "pyproject.toml", "setup.cfg", "tests", "test", "conftest.py")
    ):
        runners.append(TestRunner(label="pytest", command=[pytest_bin, "-x", "-q", "--tb=short"]))

    if (root / "package.json").exists():
        try:
            import json
            pkg = json.loads((root / "package.json").read_text())
            scripts = pkg.get("scripts", {})
            if "vitest" in _raw_deps(pkg):
                runners.append(TestRunner(label="vitest", command=["npx", "vitest", "run", "--reporter=basic"]))
            if "jest" in _raw_deps(pkg):
                runners.append(TestRunner(label="jest", command=["npx", "jest", "--silent"]))
            if not any(r.label in ("vitest", "jest") for r in runners) and "test" in scripts:
                runners.append(TestRunner(label="npm test", command=["npm", "run", "test", "--silent"]))
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    if (root / "go.mod").exists() and _which("go"):
        runners.append(TestRunner(label="go test", command=["go", "test", "./..."]))

    if (root / "Cargo.toml").exists() and _which("cargo"):
        runners.append(TestRunner(label="cargo test", command=["cargo", "test", "--quiet"]))

    makefile = root / "Makefile"
    if makefile.is_file() and _which("make"):
        try:
            if "test:" in makefile.read_text():
                runners.append(TestRunner(label="make test", command=["make", "test"]))
        except OSError:
            pass
    return runners


def _raw_deps(pkg: dict) -> str:
    bag = []
    for key in ("dependencies", "devDependencies"):
        bag.extend(pkg.get(key, {}).keys())
    return " ".join(bag)


def detect_toolchain(cwd: str) -> Toolchain:
    chain = Toolchain()
    for name, (category, version_cmd, _) in TOOL_SPECS.items():
        path = _which(name)
        if not path:
            continue
        version = _version(version_cmd, path)
        chain.tools.append(Tool(name=name, category=category, version=version, path=path))
    chain.tools.sort(key=lambda t: (t.category, t.name))
    chain.test_runners = _detect_test_runners(cwd)
    chain.container = _detect_container()
    return chain


def toolchain_section(chain: Toolchain, cwd: str) -> str:
    """The markdown # Toolchain block injected into the system prompt."""
    lines = ["# Toolchain\n",
             "Detected tooling available on this machine — interface with it natively",
             "(call the real tools and their subcommands; do not ask the user to run them):\n"]
    categories: dict[str, list[Tool]] = {}
    for tool in chain.tools:
        categories.setdefault(tool.category, []).append(tool)
    if not categories:
        lines.append("(no local dev tooling detected)")
    for category, tools in categories.items():
        names = ", ".join(f"{t.name} {('*' if t.version else '')}{t.version}".strip()
                          for t in tools)
        lines.append(f"- **{category}**: {names}")
    if chain.test_runners:
        for runner in chain.test_runners:
            extra = f" ({runner.note})" if runner.note else ""
            lines.append(f"- **test runner ({runner.label})**: `{' '.join(runner.command)}`{extra}")
        lines.append("Prefer the `test` tool, which drives these runners natively.")
    else:
        lines.append("- test runner: none detected in this project; use bash for ad-hoc checks")
    if chain.container:
        c = chain.container
        compose = f" with compose (`{' '.join(c.compose_command)}`)" if c.compose_command else ""
        lines.append(f"- **container runtime**: `{c.binary}` {c.version}{compose}")
        lines.append("Prefer the `container` tool, which drives this runtime natively.")
    else:
        lines.append("- container runtime: none detected (neither docker nor podman on PATH)")
    lines.append("")  # trailing blank line
    return "\n".join(lines)