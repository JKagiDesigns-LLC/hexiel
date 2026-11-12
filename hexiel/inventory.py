"""What this machine can do: programs on PATH, importable Python packages, OS/CPU/RAM/GPU.

One walk over the PATH directories (no version calls, so it takes milliseconds), cached on disk by a hash of PATH
and the directories' mtimes. Only the slice that matters for a request goes into the conversation — "convert this
video" brings ffmpeg's line (or how to install it), a question about a spreadsheet brings duckdb/openpyxl — and the
`have` tool answers "is X installed, and if not how do I get it" without a shell probe."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import sys
import threading
from pathlib import Path

from .config import home_data_dir

# category -> (request keywords, programs, python packages)
CATALOG: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "formatters/linters": (("formatter", "linter", "lint", "prettier", "eslint", "clippy", "typecheck", "type check"),
                           ("ruff", "black", "prettier", "eslint", "biome", "gofmt", "rustfmt", "shellcheck", "shfmt",
                            "mypy", "pyright", "clang-format", "stylua", "rubocop", "hadolint", "yamllint"), ()),
    "language servers": (("lsp", "language server", "go to definition", "diagnostic"),
                         ("pyright-langserver", "basedpyright", "pylsp", "typescript-language-server", "gopls",
                          "rust-analyzer", "clangd", "lua-language-server", "jdtls"), ()),
    "images/OCR": (("image", "photo", "picture", "screenshot", "png", "jpg", "jpeg", "ocr", "resize", "thumbnail", "svg"),
                   ("magick", "convert", "tesseract", "zbarimg", "inkscape", "rsvg-convert", "optipng", "pngquant",
                    "exiftool", "cwebp"), ("PIL", "cv2", "pytesseract")),
    "audio/video": (("video", "audio", "mp4", "mp3", "wav", "transcode", "convert video", "gif", "frames", "subtitle",
                     "transcribe"),
                    ("ffmpeg", "ffprobe", "sox", "yt-dlp", "whisper", "whisper-cli"), ("whisper", "faster_whisper")),
    "documents": (("pdf", "docx", "word", "markdown", "pandoc", "latex", "epub", "slides", "pptx", "spreadsheet",
                   "excel", "xlsx", "csv"),
                  ("pandoc", "pdftotext", "pdftoppm", "qpdf", "libreoffice", "soffice", "typst", "tectonic", "pdflatex"),
                  ("pypdf", "openpyxl", "duckdb", "docx", "pptx", "pandas", "polars", "fitz", "pdfplumber")),
    "data tools": (("json", "yaml", "sql", "database", "jq", "parquet", "sqlite", "csv"),
                   ("jq", "yq", "sqlite3", "duckdb", "psql", "mysql", "redis-cli", "mongosh", "xsv", "qsv", "mlr"),
                   ("duckdb", "sqlalchemy", "psycopg", "psycopg2", "pymysql")),
    "search": (("ripgrep", "fuzzy find", "ctags"), ("rg", "fd", "fzf", "ag", "ctags"), ()),
    "git hosting": (("pull request", "merge request", "github", "gitlab"),
                    ("gh", "glab", "tea", "hub"), ()),
    "cloud/devops": (("deploy", "aws", "gcp", "azure", "kubernetes", "k8s", "terraform", "docker", "container",
                      "helm", "ansible"),
                     ("aws", "gcloud", "az", "kubectl", "helm", "terraform", "tofu", "pulumi", "ansible", "flyctl",
                      "vercel", "netlify", "wrangler", "docker", "podman", "ssh", "rsync"), ()),
    "browsers/web": (("browser", "headless", "render", "web page", "html", "playwright", "selenium", "screenshot of"),
                     ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "firefox",
                      "microsoft-edge", "wkhtmltopdf", "curl", "wget"), ("playwright", "selenium", "requests", "bs4")),
    "build": (("cmake", "makefile", "gradle", "maven", "bazel", "bundler"),
              ("make", "cmake", "ninja", "meson", "bazel", "gradle", "mvn", "msbuild", "vite", "esbuild", "webpack"), ()),
    "ML": (("train a model", "gpu", "cuda", "torch", "fine-tune", "finetune"),
           ("nvidia-smi", "rocm-smi", "ollama", "llama-server"), ("torch", "numpy", "transformers", "sklearn")),
}

# how to get a missing program, when the system package name differs or there is a better route
INSTALL_AS = {"magick": "imagemagick", "convert": "imagemagick", "rg": "ripgrep", "fd": "fd", "zbarimg": "zbar",
              "pdftotext": "poppler-utils", "pdftoppm": "poppler-utils", "gh": "gh", "glab": "glab",
              "rsvg-convert": "librsvg", "sqlite3": "sqlite3", "yt-dlp": "yt-dlp"}
PIP_AS = {"PIL": "pillow", "cv2": "opencv-python-headless", "fitz": "pymupdf", "docx": "python-docx",
          "pptx": "python-pptx", "sklearn": "scikit-learn", "bs4": "beautifulsoup4", "faster_whisper": "faster-whisper"}

_LOCK = threading.Lock()
_CACHE: dict | None = None


def _path_dirs() -> list[str]:
    seen, out = set(), []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d and d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
    return out


def _stamp(dirs: list[str]) -> str:
    h = hashlib.sha256()
    for d in dirs:
        try:
            h.update(f"{d}:{os.stat(d).st_mtime_ns};".encode())
        except OSError:
            pass
    h.update(sys.executable.encode())
    return h.hexdigest()[:20]


def _executables(dirs: list[str]) -> dict[str, str]:
    """name -> full path for everything runnable on PATH (first one wins, like the shell)."""
    exts = [e.lower() for e in os.environ.get("PATHEXT", ".EXE;.CMD;.BAT").split(";")] if os.name == "nt" else []
    found: dict[str, str] = {}
    for d in dirs:
        try:
            with os.scandir(d) as it:
                for e in it:
                    name = e.name
                    if os.name == "nt":
                        stem, ext = os.path.splitext(name)
                        if ext.lower() not in exts:
                            continue
                        name = stem
                    elif not os.access(e.path, os.X_OK):
                        continue
                    found.setdefault(name, e.path)
        except OSError:
            continue
    return found


def _system() -> dict:
    info = {"os": f"{platform.system()} {platform.release()}", "arch": platform.machine(),
            "cpus": os.cpu_count() or 0, "python": platform.python_version()}
    try:
        if sys.platform.startswith("linux"):
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    info["ram_gb"] = round(int(line.split()[1]) / 1024 / 1024, 1)
                    break
        elif sys.platform == "darwin":
            import subprocess  # nosec B404
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=3)  # nosec
            info["ram_gb"] = round(int(out.stdout.strip()) / 1024 ** 3, 1)
    except (OSError, ValueError, Exception):  # noqa: BLE001 - facts are a bonus
        pass
    return info


def scan(force: bool = False) -> dict:
    """The inventory: {"stamp", "programs": {name: path} for the catalog, "packages": [...], "system": {...}}.
    Cached in memory and on disk until PATH (or a PATH directory) changes."""
    global _CACHE
    dirs = _path_dirs()
    stamp = _stamp(dirs)
    with _LOCK:
        if _CACHE and _CACHE.get("stamp") == stamp and not force:
            return _CACHE
        store = home_data_dir() / "inventory.json"
        if not force:
            try:
                data = json.loads(store.read_text())
                if data.get("stamp") == stamp:
                    _CACHE = data
                    return data
            except (OSError, ValueError):
                pass
        exe = _executables(dirs)
        wanted = {p for _kw, progs, _pk in CATALOG.values() for p in progs}
        programs = {name: exe[name] for name in sorted(wanted) if name in exe}
        pkgs = sorted({p for _kw, _pr, ps in CATALOG.values() for p in ps if _importable(p)})
        data = {"stamp": stamp, "programs": programs, "packages": pkgs, "system": _system(),
                "all_count": len(exe)}
        _CACHE = data
        try:
            store.parent.mkdir(parents=True, exist_ok=True)
            store.write_text(json.dumps(data))
        except OSError:
            pass
        return data


def _importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def install_hint(name: str) -> str:
    from .doctor import _pip_install, _pkg_manager_install
    if name in PIP_AS or (name.isidentifier() and name.islower() and name not in INSTALL_AS
                          and any(name in ps for _k, _p, ps in CATALOG.values())):
        return _pip_install(PIP_AS.get(name, name))
    return _pkg_manager_install(INSTALL_AS.get(name, name))


def have(name: str) -> str:
    """'name: /path' when installed, else 'name: not installed — <install command>'. Any program, not just the
    catalog (a PATH lookup), and Python packages by import name."""
    name = name.strip()
    inv = scan()
    path = inv["programs"].get(name) or shutil.which(name)
    if path:
        return f"{name}: installed at {path}"
    if name in inv["packages"] or (name.isidentifier() and _importable(name)):
        return f"{name}: importable Python package (in {sys.executable})"
    return f"{name}: not installed — {install_hint(name)}"


def relevant(text: str, limit: int = 3) -> str:
    """The inventory lines this request needs: categories whose keywords appear in it, with what is installed
    and what is missing (and how to install it). "" when nothing matches — the common case costs nothing."""
    low = f" {text.lower()} "
    hits = [cat for cat, (kws, _p, _k) in CATALOG.items() if any(re.search(rf"\b{re.escape(k.strip())}\b", low)
                                                                      for k in kws)]
    if not hits:
        return ""
    inv = scan()
    lines = []
    for cat in hits[:limit]:
        _kws, progs, pkgs = CATALOG[cat]
        have_p = [p for p in progs if p in inv["programs"]]
        have_k = [p for p in pkgs if p in inv["packages"]]
        got = ", ".join(have_p + [f"python:{p}" for p in have_k]) or "nothing"
        missing = [p for p in progs[:4] if p not in inv["programs"]]
        tail = f"; missing: {', '.join(missing)} (use the have tool for the install command)" if missing and not have_p else ""
        lines.append(f"- {cat}: {got}{tail}")
    return "[installed on this machine, for this request]\n" + "\n".join(lines)


def system_line() -> str:
    s = scan()["system"]
    ram = f", {s['ram_gb']} GB RAM" if s.get("ram_gb") else ""
    gpu = ", NVIDIA GPU" if "nvidia-smi" in scan()["programs"] else ""
    return f"{s.get('os', '?')} {s.get('arch', '')}, {s.get('cpus', '?')} CPUs{ram}{gpu}, Python {s.get('python', '?')}"
