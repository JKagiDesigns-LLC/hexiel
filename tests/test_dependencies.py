"""Every third-party package hexiel imports must be installed by `pip install hexiel` / the installer, or be a
deliberately optional one — so a new built-in tool cannot ship needing a package nobody installs."""

import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# import name -> why it may be missing (each is imported inside try/except and has a fallback)
OPTIONAL = {"keyring": "OS keychain for API keys (discover.py)", "pyzbar": "barcode reading needs the zbar system library",
            "qrcode": "fallback only: segno is the core QR library", "aiohttp": "serve extra (the installer adds it)"}
# transitive: installed because a declared dependency requires them
TRANSITIVE = {"rich": "textual", "pygments": "rich", "httpcore": "httpx", "numpy": "see extra"}
IMPORT_NAMES = {"pillow": "PIL", "pillow-heif": "pillow_heif", "imageio-ffmpeg": "imageio_ffmpeg", "tree-sitter": "tree_sitter",
                "tree-sitter-language-pack": "tree_sitter_language_pack", "textual-image": "textual_image",
                "rapidocr-onnxruntime": "rapidocr_onnxruntime"}


def declared() -> set[str]:
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    reqs = list(meta["dependencies"])
    for extra in meta["optional-dependencies"].values():
        reqs += extra
    names = set()
    for r in reqs:
        name = re.split(r"[<>=\[; ]", r.strip(), maxsplit=1)[0].lower()
        if name and name != "hexiel":
            names.add(IMPORT_NAMES.get(name, name.replace("-", "_")))
    return names


def imported() -> dict[str, str]:
    out = {}
    for f in (ROOT / "hexiel").rglob("*.py"):
        for n in ast.walk(ast.parse(f.read_text())):
            mods = [a.name.split(".")[0] for a in n.names] if isinstance(n, ast.Import) else \
                [n.module.split(".")[0]] if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module else []
            for m in mods:
                if m not in sys.stdlib_module_names and m != "hexiel":
                    out.setdefault(m, f.name)
    return out


def test_every_import_is_declared_or_deliberately_optional():
    known = declared() | set(OPTIONAL) | set(TRANSITIVE)
    missing = {m: f for m, f in imported().items() if m not in known}
    assert not missing, f"imported but not in pyproject.toml (add it, or list it as optional here): {missing}"


def test_the_installers_ask_for_every_extra_the_tools_use():
    for script in ("install/install.sh", "install/install.ps1"):
        text = (ROOT / script).read_text()
        for extra in ("serve", "see", "code", "view"):
            assert extra in re.search(r"hexiel\[([a-z,]+)\]", text).group(1).split(","), f"{script} lacks the {extra} extra"
