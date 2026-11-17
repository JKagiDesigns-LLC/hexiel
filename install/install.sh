#!/bin/sh
# hexiel installer for macOS and Linux.
#
#     curl -fsSL https://hexiel.tech/install.sh | sh
#
# What it does, in this order, and nothing else:
#   1. refuses to run as root (hexiel is a per-user tool)
#   2. installs uv (https://astral.sh/uv) into ~/.local/bin if there is no uv and no Python 3.11+ —
#      a pinned release, downloaded from GitHub with its .sha256 file and verified before use
#   3. installs hexiel with `uv tool install` (uv brings its own Python when the system has none),
#      or with pipx / a virtualenv when you ask for that (HEXIEL_INSTALL_METHOD=pipx|venv)
#   4. makes sure ~/.local/bin is on your PATH for new terminals
#   5. checks the built-in tools' dependencies (Pillow, ffmpeg … all come with the install; no admin rights needed)
#      and, only when it can do so without a password prompt, adds tesseract for screen OCR (HEXIEL_NO_SYSTEM_DEPS=1 skips)
#   6. runs `hexiel --setup`: finds the keys and agents already on this machine and ends in a working chat
#
# Variables: HEXIEL_INSTALL_METHOD=uv|pipx|venv (default uv) · HEXIEL_VERSION=0.9.3 (default: latest)
#            HEXIEL_NO_VISION_MODEL=1 (skip the one-time 265 MB local vision model download)
#            HEXIEL_SPEC='.[serve,see]' (install this checkout instead of PyPI) · HEXIEL_NO_SETUP=1 (skip step 6)
#            UV_VERSION (default pinned below)
set -eu

UV_VERSION="${UV_VERSION:-0.13.7}"
HEXIEL_SPEC="${HEXIEL_SPEC:-hexiel[serve,see,code,view]}"            # a path or git URL works too (CI installs the tree under test)
[ -n "${HEXIEL_VERSION:-}" ] && HEXIEL_SPEC="hexiel[serve,see,code,view]==${HEXIEL_VERSION}"
METHOD="${HEXIEL_INSTALL_METHOD:-uv}"
BIN="$HOME/.local/bin"

say()  { printf '%s\n' "$*"; }
fail() { printf 'hexiel install: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

[ "$(id -u)" = "0" ] && [ -z "${HEXIEL_ALLOW_ROOT:-}" ] && \
  fail "do not run this as root — run it as the user who will use hexiel (set HEXIEL_ALLOW_ROOT=1 to override)"
have curl || fail "curl is required"
mkdir -p "$BIN"

# ---------------------------------------------------------------- a Python 3.11+?
python_ok() {
  for py in python3 python; do
    if have "$py" && "$py" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      PY="$py"; return 0
    fi
  done
  return 1
}

# ---------------------------------------------------------------- uv, verified
install_uv() {
  os=$(uname -s); arch=$(uname -m)
  case "$os" in
    Darwin) target="apple-darwin" ;;
    Linux)  if ldd --version 2>&1 | grep -qi musl; then target="unknown-linux-musl"; else target="unknown-linux-gnu"; fi ;;
    *) fail "unsupported OS: $os (Windows: irm https://hexiel.tech/install.ps1 | iex)" ;;
  esac
  case "$arch" in
    x86_64|amd64) arch="x86_64" ;;
    arm64|aarch64) arch="aarch64" ;;
    *) fail "unsupported CPU: $arch" ;;
  esac
  name="uv-${arch}-${target}.tar.gz"
  base="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}"
  tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
  say "downloading uv ${UV_VERSION} (${name}) …"
  curl -fsSL "${base}/${name}" -o "$tmp/$name"
  curl -fsSL "${base}/${name}.sha256" -o "$tmp/$name.sha256"
  expected=$(awk '{print $1}' "$tmp/$name.sha256")
  if have sha256sum; then actual=$(sha256sum "$tmp/$name" | awk '{print $1}');
  elif have shasum; then actual=$(shasum -a 256 "$tmp/$name" | awk '{print $1}');
  else fail "neither sha256sum nor shasum found to verify the download"; fi
  [ "$expected" = "$actual" ] || fail "checksum mismatch for $name (expected $expected, got $actual) — not installing"
  tar -xzf "$tmp/$name" -C "$tmp"
  cp "$tmp"/uv-*/uv "$tmp"/uv-*/uvx "$BIN"/ 2>/dev/null || cp "$tmp"/uv "$tmp"/uvx "$BIN"/
  chmod +x "$BIN/uv" "$BIN/uvx"
  say "uv ${UV_VERSION} installed to $BIN (checksum verified)"
}

# ---------------------------------------------------------------- install hexiel
case "$METHOD" in
  uv)
    if ! have uv && ! [ -x "$BIN/uv" ]; then install_uv; fi
    UV=$(command -v uv 2>/dev/null || echo "$BIN/uv")
    say "installing $HEXIEL_SPEC with uv …"
    "$UV" tool install --python ">=3.11" --force "$HEXIEL_SPEC"
    "$UV" tool update-shell >/dev/null 2>&1 || true
    ;;
  pipx)
    have pipx || fail "pipx is not installed (pipx.pypa.io) — or let the default (uv) handle it"
    pipx install --force "$HEXIEL_SPEC"
    pipx ensurepath >/dev/null 2>&1 || true
    ;;
  venv)
    python_ok || fail "a Python 3.11+ is needed for HEXIEL_INSTALL_METHOD=venv — or let the default (uv) bring one"
    "$PY" -m venv "$HOME/.hexiel-venv"
    "$HOME/.hexiel-venv/bin/pip" install --upgrade pip >/dev/null
    "$HOME/.hexiel-venv/bin/pip" install --upgrade "$HEXIEL_SPEC"
    ln -sf "$HOME/.hexiel-venv/bin/hexiel" "$BIN/hexiel"
    ;;
  *) fail "HEXIEL_INSTALL_METHOD must be uv, pipx or venv" ;;
esac

# ---------------------------------------------------------------- PATH for new terminals
case ":$PATH:" in
  *":$BIN:"*) ;;
  *)
    for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.profile"; do
      [ -f "$rc" ] || continue
      grep -q 'local/bin' "$rc" 2>/dev/null || printf '\n# added by the hexiel installer\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$rc"
    done
    if [ -d "$HOME/.config/fish" ]; then
      mkdir -p "$HOME/.config/fish/conf.d"
      [ -f "$HOME/.config/fish/conf.d/hexiel.fish" ] || printf 'fish_add_path -g $HOME/.local/bin\n' > "$HOME/.config/fish/conf.d/hexiel.fish"
    fi
    say "added ~/.local/bin to your PATH for new terminals"
    ;;
esac
export PATH="$BIN:$PATH"

HEXIEL=$(command -v hexiel 2>/dev/null || echo "$BIN/hexiel")
[ -x "$HEXIEL" ] || fail "hexiel was installed but not found on PATH — open a new terminal and run: hexiel --doctor"
say ""
say "✓ $("$HEXIEL" --version) installed at $HEXIEL"

# ---------------------------------------------------------------- tools the built-ins use
# Pillow, segno, pillow-heif and a bundled ffmpeg are ordinary dependencies of hexiel, so they are already in.
# tesseract (screen OCR) is the one system program: installed only when a package manager can do it unattended.
tool_python() {
  for d in "$HOME/.local/share/uv/tools/hexiel" "$HOME/.hexiel-venv"; do
    [ -x "$d/bin/python" ] && { echo "$d/bin/python"; return; }
  done
  command -v python3 || true
}
TP=$(tool_python)
if [ -n "$TP" ] && ! "$TP" -c 'import PIL, segno, imageio_ffmpeg' 2>/dev/null; then
  say "note: some built-in tool libraries are missing — run: hexiel --doctor"
fi
if [ -z "${HEXIEL_NO_SYSTEM_DEPS:-}" ] && ! have tesseract; then
  SUDO=""; [ "$(id -u)" != "0" ] && have sudo && sudo -n true 2>/dev/null && SUDO="sudo -n"
  if have brew; then brew install tesseract >/dev/null 2>&1 && say "installed tesseract (screen OCR)" || true
  elif [ -n "$SUDO" ] || [ "$(id -u)" = "0" ]; then
    if have apt-get; then $SUDO apt-get install -y tesseract-ocr >/dev/null 2>&1
    elif have dnf; then $SUDO dnf install -y tesseract >/dev/null 2>&1
    elif have pacman; then $SUDO pacman -S --noconfirm tesseract tesseract-data-eng >/dev/null 2>&1
    elif have zypper; then $SUDO zypper -n install tesseract-ocr >/dev/null 2>&1
    elif have apk; then $SUDO apk add tesseract-ocr >/dev/null 2>&1
    fi && have tesseract && say "installed tesseract (screen OCR)" || true
  fi
  have tesseract || say "optional: install tesseract for screen OCR (brew install tesseract / sudo apt install tesseract-ocr)"
fi

# local vision model (Florence-2, ~265 MB, one time): lets text-only models "see" pictures. Opt out: HEXIEL_NO_VISION_MODEL=1
if [ -z "${HEXIEL_NO_VISION_MODEL:-}" ]; then
  say "downloading the local vision model (~265 MB, once) …"
  "$HEXIEL" --get-vision || say "note: skipped the vision model — run later: hexiel --get-vision"
fi

# ---------------------------------------------------------------- first run
if [ -z "${HEXIEL_NO_SETUP:-}" ]; then
  if [ -r /dev/tty ]; then
    "$HEXIEL" --setup </dev/tty || true
  else
    say "run `hexiel --setup` to finish (it finds your keys and picks a model that answers)"
  fi
else
  say "next: hexiel --setup"
fi
