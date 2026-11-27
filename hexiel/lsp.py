"""Language-server support: real "go to definition / find references / hover / diagnostics" answers from any
language server installed on the machine, so the model reads one precise answer instead of grepping and
opening files.

The idea (not the code) follows Helix and Neovim's LSP setups: a server is declared once, by name, with the
command to start it, the file types it handles and the files that mark a project root (`pyproject.toml`,
`go.mod`, ...). The built-in table below covers the common languages; `~/.config/hexiel/lsp.toml` and a
project's `.hexiel/lsp.toml` add or override entries, with the same shape:

    [server.pyright]
    command = ["pyright-langserver", "--stdio"]
    extensions = [".py", ".pyi"]
    root_markers = ["pyproject.toml", "setup.py", ".git"]
    install = "pipx install pyright"

Servers start on first use for a (server, project root) pair, are shut down after IDLE_SECONDS without a
request, and are always killed when hexiel exits. Nothing is installed here: a missing server comes back as a
message with the install command, and the model falls back to hexiel's own symbol index.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

IDLE_SECONDS = 300
REQUEST_TIMEOUT = 20.0
START_TIMEOUT = 40.0
MAX_ITEMS = 40

BUILTIN = """
[server.pyright]
command = ["pyright-langserver", "--stdio"]
extensions = [".py", ".pyi"]
root_markers = ["pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", ".git"]
language_id = "python"
install = "pipx install pyright   (or: npm i -g pyright)"
install_cmds = [["pipx","install","pyright"],["uv","tool","install","pyright"],["npm","i","-g","pyright"]]

[server.typescript]
command = ["typescript-language-server", "--stdio"]
extensions = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"]
root_markers = ["tsconfig.json", "jsconfig.json", "package.json", ".git"]
language_id = "typescript"
install = "npm i -g typescript typescript-language-server"
install_cmds = [["npm","i","-g","typescript","typescript-language-server"]]

[server.gopls]
command = ["gopls"]
extensions = [".go"]
root_markers = ["go.work", "go.mod", ".git"]
language_id = "go"
install = "go install golang.org/x/tools/gopls@latest"
install_cmds = [["go","install","golang.org/x/tools/gopls@latest"]]

[server.rust-analyzer]
command = ["rust-analyzer"]
extensions = [".rs"]
root_markers = ["Cargo.toml", ".git"]
language_id = "rust"
install = "rustup component add rust-analyzer"
install_cmds = [["rustup","component","add","rust-analyzer"]]

[server.clangd]
command = ["clangd"]
extensions = [".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh"]
root_markers = ["compile_commands.json", "compile_flags.txt", ".clangd", ".git"]
language_id = "cpp"
install = "install clangd with your package manager (apt/pacman/brew: clangd)"

[server.jdtls]
command = ["jdtls"]
extensions = [".java"]
root_markers = ["pom.xml", "build.gradle", "build.gradle.kts", ".git"]
language_id = "java"
install = "brew install jdtls   (or your package manager)"
install_cmds = [["brew","install","jdtls"]]

[server.lua-language-server]
command = ["lua-language-server"]
extensions = [".lua"]
root_markers = [".luarc.json", ".git"]
language_id = "lua"
install = "brew install lua-language-server   (or your package manager)"
install_cmds = [["brew","install","lua-language-server"]]

[server.bash-language-server]
command = ["bash-language-server", "start"]
extensions = [".sh", ".bash"]
root_markers = [".git"]
language_id = "shellscript"
install = "npm i -g bash-language-server"
install_cmds = [["npm","i","-g","bash-language-server"]]

[server.ruby-lsp]
command = ["ruby-lsp"]
extensions = [".rb"]
root_markers = ["Gemfile", ".git"]
language_id = "ruby"
install = "gem install ruby-lsp"
install_cmds = [["gem","install","ruby-lsp"]]

[server.intelephense]
command = ["intelephense", "--stdio"]
extensions = [".php"]
root_markers = ["composer.json", ".git"]
language_id = "php"
install = "npm i -g intelephense"
install_cmds = [["npm","i","-g","intelephense"]]
"""


@dataclass
class ServerSpec:
    name: str
    command: list
    extensions: list
    root_markers: list = field(default_factory=lambda: [".git"])
    language_id: str = ""
    install: str = ""
    install_cmds: list = field(default_factory=list)   # argv lists, tried in order: the first whose program exists
    env: dict = field(default_factory=dict)

    def resolve(self) -> str | None:
        """Full path of the server's program, looking in the usual user install folders too (pipx, go, cargo, npm)."""
        return find_program(self.command[0]) if self.command else None

    def installed(self) -> bool:
        return self.resolve() is not None


def extra_bin_dirs() -> list[str]:
    """Where installers drop programs that are often not on PATH yet."""
    home = Path.home()
    dirs = [home / ".local" / "bin", home / ".cargo" / "bin", home / ".npm-global" / "bin", home / "go" / "bin"]
    for var, sub in (("GOBIN", ""), ("GOPATH", "bin"), ("CARGO_HOME", "bin"), ("PNPM_HOME", "")):
        if os.environ.get(var):
            dirs.append(Path(os.environ[var]) / sub)
    return [str(d) for d in dirs if d.is_dir()]


def find_program(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    return shutil.which(name, path=os.pathsep.join(extra_bin_dirs())) if extra_bin_dirs() else None


def _spec(name: str, d: dict, base: ServerSpec | None = None) -> ServerSpec | None:
    cmd = d.get("command", base.command if base else None)
    exts = d.get("extensions", base.extensions if base else None)
    if not isinstance(cmd, list) or not cmd or not isinstance(exts, list):
        return None
    return ServerSpec(
        name=name, command=[str(c) for c in cmd], extensions=[str(e).lower() for e in exts],
        root_markers=[str(m) for m in d.get("root_markers", base.root_markers if base else [".git"])],
        language_id=str(d.get("language_id", base.language_id if base else "") or ""),
        install=str(d.get("install", base.install if base else "") or ""),
        install_cmds=_cmds(d.get("install_cmds", base.install_cmds if base else [])),
        env={str(k): str(v) for k, v in (d.get("env") or {}).items()})


def _cmds(v) -> list:
    return [[str(a) for a in c] for c in v if isinstance(c, list) and c] if isinstance(v, list) else []


def project_file_trusted(project: str) -> bool:
    """A project's own lsp.toml names programs hexiel will run, so it only counts in a trusted project
    (`hexiel --trust-project`, the same switch that lets a project's plugins load)."""
    try:
        from .openwebui_compat import is_trusted
        return bool(is_trusted(project))
    except Exception:   # noqa: BLE001
        return False


def untrusted_project_note(project: str) -> str:
    if (Path(project) / ".hexiel" / "lsp.toml").is_file() and not project_file_trusted(project):
        return " (this project's .hexiel/lsp.toml is ignored until you run `hexiel --trust-project` here)"
    return ""


def load_registry(project: str | None = None, extra_files: list | None = None) -> dict[str, ServerSpec]:
    """Built-in servers, then the user's lsp.toml, then the project's: a later entry with the same name
    replaces fields of the earlier one; a new name adds a server."""
    from .config import home_config_dir
    reg: dict[str, ServerSpec] = {}
    files: list = [None, home_config_dir() / "lsp.toml"]
    if project and project_file_trusted(project):
        files.append(Path(project) / ".hexiel" / "lsp.toml")
    files += extra_files or []
    for f in files:
        try:
            data = tomllib.loads(BUILTIN) if f is None else tomllib.loads(Path(f).read_text("utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        for name, d in (data.get("server") or {}).items():
            if isinstance(d, dict):
                if d.get("disabled"):
                    reg.pop(name, None)
                    continue
                s = _spec(name, d, reg.get(name))
                if s:
                    reg[name] = s
    return reg


def server_for(path: str, reg: dict[str, ServerSpec]) -> ServerSpec | None:
    ext = Path(path).suffix.lower()
    # an installed server wins over one that is not, when two claim the same extension
    claims = [s for s in reg.values() if ext in s.extensions]
    return next((s for s in claims if s.installed()), claims[0] if claims else None)


def find_root(path: str, markers: list, stop: str | None = None) -> str:
    """Nearest parent of `path` holding one of the marker files; the opened folder when none does."""
    p = Path(path).resolve().parent
    for d in [p, *p.parents]:
        if any((d / m).exists() for m in markers):
            return str(d)
    return stop or str(p)


def to_uri(path: str) -> str:
    return "file://" + quote(str(Path(path).resolve()))


def from_uri(uri: str) -> str:
    return unquote(urlparse(uri).path)


class LspError(RuntimeError):
    pass


class Client:
    """One language server process, spoken to over stdio JSON-RPC."""

    def __init__(self, spec: ServerSpec, root: str):
        self.spec, self.root = spec, root
        self._id = 0
        self._lock = threading.Lock()
        self._pending: dict[int, dict] = {}
        self._versions: dict[str, int] = {}
        self._texts: dict[str, str] = {}
        self.diagnostics: dict[str, list] = {}
        self._diag_event = threading.Condition()
        self.last_used = time.monotonic()
        self.dead = False
        env = {**os.environ, **spec.env}
        try:
            cmd = [spec.resolve() or spec.command[0], *spec.command[1:]]
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, cwd=root, env=env, start_new_session=True)
        except OSError as e:
            raise LspError(f"could not start {spec.command[0]}: {e}") from e
        threading.Thread(target=self._read_loop, daemon=True, name=f"lsp-{spec.name}").start()
        res = self.request("initialize", {
            "processId": os.getpid(), "rootUri": to_uri(root), "rootPath": root,
            "workspaceFolders": [{"uri": to_uri(root), "name": Path(root).name}],
            "capabilities": {"textDocument": {
                "definition": {"linkSupport": True}, "references": {}, "hover": {"contentFormat": ["plaintext", "markdown"]},
                "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                "publishDiagnostics": {}, "synchronization": {"didSave": False}},
                "workspace": {"symbol": {}, "workspaceFolders": True}}}, timeout=START_TIMEOUT)
        self.capabilities = (res or {}).get("capabilities", {})
        self.notify("initialized", {})

    # ---- transport
    def _send(self, msg: dict) -> None:
        body = json.dumps(msg).encode()
        try:
            with self._lock:
                self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
                self.proc.stdin.flush()
        except (OSError, ValueError) as e:
            self.dead = True
            raise LspError(f"{self.spec.name} is not running") from e

    def _read_loop(self) -> None:
        out = self.proc.stdout
        try:
            while True:
                length = None
                while True:
                    line = out.readline()
                    if not line:
                        raise EOFError
                    line = line.strip()
                    if not line:
                        break
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1])
                if length is None:
                    continue
                msg = json.loads(out.read(length))
                self._handle(msg)
        except (EOFError, OSError, ValueError):
            pass
        finally:
            self.dead = True
            for slot in list(self._pending.values()):
                slot["event"].set()
            with self._diag_event:
                self._diag_event.notify_all()

    def _handle(self, msg: dict) -> None:
        if "id" in msg and "method" not in msg:
            slot = self._pending.get(msg["id"])
            if slot:
                slot["msg"] = msg
                slot["event"].set()
        elif msg.get("method") == "textDocument/publishDiagnostics":
            p = msg.get("params", {})
            with self._diag_event:
                self.diagnostics[from_uri(p.get("uri", ""))] = p.get("diagnostics", [])
                self._diag_event.notify_all()
        elif "id" in msg and "method" in msg:   # the server asking us something: answer so it never blocks
            m = msg["method"]
            result = None
            if m == "workspace/configuration":
                result = [{} for _ in (msg.get("params", {}).get("items") or [None])]
            try:
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
            except LspError:
                pass

    def request(self, method: str, params: dict, timeout: float = REQUEST_TIMEOUT, stop=None):
        with self._lock:
            self._id += 1
            rid = self._id
        slot = {"event": threading.Event(), "msg": None}
        self._pending[rid] = slot
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            deadline = time.monotonic() + timeout
            while not slot["event"].wait(0.1):
                if stop is not None and stop.is_set():
                    raise LspError("cancelled")
                if time.monotonic() > deadline:
                    raise LspError(f"{self.spec.name} did not answer {method} in {timeout:.0f}s (still indexing?)")
            if slot["msg"] is None:
                raise LspError(f"{self.spec.name} exited")
            err = slot["msg"].get("error")
            if err:
                raise LspError(f"{self.spec.name}: {err.get('message', err)}")
            self.last_used = time.monotonic()
            return slot["msg"].get("result")
        finally:
            self._pending.pop(rid, None)

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # ---- documents
    def sync(self, path: str, language_id: str) -> str:
        """Make the server's copy of the file match the disk; returns the text."""
        path = str(Path(path).resolve())
        text = Path(path).read_text("utf-8", "replace")
        uri = to_uri(path)
        if path not in self._versions:
            self._versions[path] = 1
            self.notify("textDocument/didOpen", {"textDocument": {
                "uri": uri, "languageId": language_id or self.spec.language_id, "version": 1, "text": text}})
        elif self._texts.get(path) != text:
            self._versions[path] += 1
            self.diagnostics.pop(path, None)
            self.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": self._versions[path]},
                                                   "contentChanges": [{"text": text}]})
        self._texts[path] = text
        return text

    def wait_diagnostics(self, path: str, timeout: float = 8.0) -> list:
        path = str(Path(path).resolve())
        end = time.monotonic() + timeout
        with self._diag_event:
            while path not in self.diagnostics and not self.dead:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self._diag_event.wait(left)
            return list(self.diagnostics.get(path, []))

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.request("shutdown", {}, timeout=1.5)
                self.notify("exit", {})
            except LspError:
                pass
        try:
            self.proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        if self.proc.poll() is None:
            try:
                if self.proc.pid <= 1:
                    raise ProcessLookupError
                os.killpg(self.proc.pid, 9)
            except (OSError, ProcessLookupError):
                self.proc.kill()
        self.dead = True


class Manager:
    """The language servers one hexiel process has open. Shared by all sessions (a server for a project root
    is expensive to start); each is reaped after IDLE_SECONDS and all die at exit."""

    def __init__(self):
        self._clients: dict[tuple, Client] = {}
        self._lock = threading.Lock()
        self._reaper: threading.Thread | None = None
        atexit.register(self.shutdown)

    def get(self, spec: ServerSpec, root: str) -> Client:
        key = (spec.name, root)
        with self._lock:
            c = self._clients.get(key)
            if c and c.dead:
                self._clients.pop(key, None)
                c = None
            if c is None:
                c = Client(spec, root)
                self._clients[key] = c
                self._start_reaper()
            return c

    def _start_reaper(self) -> None:
        if self._reaper and self._reaper.is_alive():
            return
        self._reaper = threading.Thread(target=self._reap_loop, daemon=True, name="lsp-reaper")
        self._reaper.start()

    def _reap_loop(self) -> None:
        while True:
            time.sleep(15)
            with self._lock:
                stale = [k for k, c in self._clients.items() if c.dead or time.monotonic() - c.last_used > IDLE_SECONDS]
                gone = [self._clients.pop(k) for k in stale]
                empty = not self._clients
            for c in gone:
                c.close()
            if empty:
                return

    def shutdown(self) -> None:
        with self._lock:
            gone = list(self._clients.values())
            self._clients.clear()
        for c in gone:
            try:
                c.proc.kill() if c.proc.poll() is None else None
            except OSError:
                pass

    def running(self) -> list[str]:
        with self._lock:
            return [f"{n} ({r})" for (n, r), c in self._clients.items() if not c.dead]


MANAGER = Manager()

# ------------------------------------------------------------------ formatting

SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}


def _rel(path: str, cwd: str) -> str:
    try:
        return os.path.relpath(path, cwd) if path.startswith(cwd) else path
    except ValueError:
        return path


def _line_of(path: str, line0: int) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, text in enumerate(f):
                if i == line0:
                    return text.strip()[:160]
    except OSError:
        pass
    return ""


def fmt_locations(locs, cwd: str) -> str:
    if not locs:
        return ""
    if isinstance(locs, dict):
        locs = [locs]
    lines, seen = [], set()
    for loc in locs:
        uri = loc.get("uri") or loc.get("targetUri")
        rng = loc.get("range") or loc.get("targetSelectionRange") or {}
        if not uri:
            continue
        p, ln = from_uri(uri), rng.get("start", {}).get("line", 0)
        if (p, ln) in seen:
            continue
        seen.add((p, ln))
        lines.append(f"{_rel(p, cwd)}:{ln + 1}  {_line_of(p, ln)}")
    more = f"\n… {len(lines) - MAX_ITEMS} more" if len(lines) > MAX_ITEMS else ""
    return "\n".join(lines[:MAX_ITEMS]) + more


def fmt_hover(h) -> str:
    if not h:
        return ""
    c = h.get("contents")
    if isinstance(c, dict):
        c = c.get("value", "")
    elif isinstance(c, list):
        c = "\n".join(x.get("value", "") if isinstance(x, dict) else str(x) for x in c)
    return str(c or "").strip()[:2000]


def fmt_diagnostics(diags: list, path: str, cwd: str) -> str:
    if not diags:
        return "no diagnostics (clean)"
    rows = []
    for d in sorted(diags, key=lambda d: (d.get("severity", 1), d.get("range", {}).get("start", {}).get("line", 0))):
        ln = d.get("range", {}).get("start", {}).get("line", 0) + 1
        rows.append(f"{_rel(path, cwd)}:{ln}  {SEVERITY.get(d.get('severity', 1), 'error')}: {str(d.get('message', '')).splitlines()[0][:200]}")
    more = f"\n… {len(rows) - MAX_ITEMS} more" if len(rows) > MAX_ITEMS else ""
    return f"{len(diags)} diagnostic(s)\n" + "\n".join(rows[:MAX_ITEMS]) + more


def fmt_symbols(items, cwd: str) -> str:
    rows = []
    for s in items or []:
        loc = s.get("location") or {}
        uri = loc.get("uri")
        if not uri:
            continue
        ln = loc.get("range", {}).get("start", {}).get("line", 0) + 1
        cont = f" in {s['containerName']}" if s.get("containerName") else ""
        rows.append(f"{s.get('name')}{cont}  {_rel(from_uri(uri), cwd)}:{ln}")
    return "\n".join(rows[:MAX_ITEMS])


def locate(text: str, symbol: str, line: int | None) -> tuple[int, int] | None:
    """(line0, col0) of `symbol` on the 1-based `line`, or its first whole-word use in the file."""
    import re
    pat = re.compile(r"(?<![\w$])" + re.escape(symbol) + r"(?![\w$])")
    lines = text.splitlines()
    order = ([line - 1] if line and 1 <= line <= len(lines) else []) + [i for i in range(len(lines)) if i != (line or 0) - 1]
    for i in order:
        m = pat.search(lines[i])
        if m:
            return i, m.start()
    return None


# ------------------------------------------------------------------ the tool

ACTIONS = ("definition", "references", "hover", "diagnostics", "symbols")


def run_action(args: dict, cwd: str, stop=None, manager: Manager | None = None) -> tuple[str, bool]:
    """Answer one `lsp` tool call. Returns (text, is_error)."""
    mgr = manager or MANAGER
    action = str(args.get("action", "")).strip().lower()
    if action not in ACTIONS:
        return f"error: action must be one of {', '.join(ACTIONS)}", True
    reg = load_registry(cwd)
    query = str(args.get("query", "")).strip()
    raw = str(args.get("path", "")).strip()
    if action == "symbols" and not raw:
        # workspace search needs some server: use the one for the project's main language
        spec = next((s for s in reg.values() if s.installed()), None)
        if not spec:
            return "no language server installed — " + _hints(reg), True
        root = cwd
    else:
        if not raw:
            return "error: path is required", True
        path = os.path.expanduser(raw)
        path = path if os.path.isabs(path) else os.path.join(cwd, path)
        if not os.path.isfile(path):
            return f"error: no such file: {raw}", True
        spec = server_for(path, reg)
        if spec is None:
            return (f"no language server is configured for {Path(path).suffix or 'this file type'} files. Add one in "
                    f"~/.config/hexiel/lsp.toml, or use where_is / callers / outline." + untrusted_project_note(cwd)), True
        if not spec.installed():
            return (f"{spec.name} is not installed, so no live code intelligence for {Path(path).suffix} files. "
                    f"Install: {spec.install or 'see its docs'}" + (f" — or call lsp_install server={spec.name!r} (asks the user first)" if spec.install_cmds else "")
                    + ". Meanwhile use where_is / callers / outline." + untrusted_project_note(cwd)), True
        root = find_root(path, spec.root_markers, cwd)
    try:
        client = mgr.get(spec, root)
        if action == "symbols":
            if not query:
                return "error: symbols needs `query`", True
            res = client.request("workspace/symbol", {"query": query}, stop=stop)
            return fmt_symbols(res, cwd) or f"no symbol matching {query!r}", False
        text = client.sync(path, spec.language_id)
        uri = to_uri(path)
        if action == "diagnostics":
            return fmt_diagnostics(client.wait_diagnostics(path), path, cwd), False
        symbol = str(args.get("symbol", "")).strip()
        line = args.get("line")
        line = int(line) if line not in (None, "") else None
        col = args.get("column")
        if symbol:
            pos = locate(text, symbol, line)
            if pos is None:
                return f"error: {symbol!r} does not appear in {raw}", True
        elif line is not None:
            pos = (line - 1, (int(col) - 1) if col not in (None, "") else 0)
            if col in (None, ""):   # no column: first identifier on the line
                import re
                src = text.splitlines()[line - 1] if 1 <= line <= len(text.splitlines()) else ""
                m = re.search(r"[A-Za-z_$][\w$]*", src)
                pos = (line - 1, m.start() if m else 0)
        else:
            return "error: give `symbol` (the name; add `line` to pick between uses) or `line` (+ `column`)", True
        params = {"textDocument": {"uri": uri}, "position": {"line": pos[0], "character": pos[1]}}
        if action == "definition":
            out = fmt_locations(client.request("textDocument/definition", params, stop=stop), cwd)
            return out or "no definition found", False
        if action == "references":
            params["context"] = {"includeDeclaration": False}
            res = client.request("textDocument/references", params, stop=stop)
            out = fmt_locations(res, cwd)
            return (f"{len(res)} reference(s)\n{out}" if out else "no references found"), False
        out = fmt_hover(client.request("textDocument/hover", params, stop=stop))
        return out or "no hover information", False
    except LspError as e:
        return f"error: {e}", True


def _hints(reg: dict) -> str:
    return "; ".join(f"{s.name}: {s.install}" for s in list(reg.values())[:4] if s.install)


def doctor_lines(cwd: str) -> list[tuple[str, bool, str]]:
    """(server, installed, install hint) for the servers whose languages this project uses."""
    reg = load_registry(cwd)
    used: set = set()
    skip = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target"}
    n = 0
    for dirpath, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        for f in files:
            n += 1
            s = server_for(f, reg)
            if s:
                used.add(s.name)
        if n > 4000:
            break
    return [(reg[k].name, reg[k].installed(), reg[k].install) for k in sorted(used)]


# ------------------------------------------------------------------ installing

def pick_recipe(spec: ServerSpec) -> list | None:
    """The first install command whose program is available on this machine."""
    return next((c for c in spec.install_cmds if find_program(c[0])), None)


def install_server(spec: ServerSpec, approve, stop=None, timeout: float = 900.0, popen=subprocess.Popen) -> tuple[str, bool]:
    """Install `spec` with the first available package manager, after `approve(command_text)` says yes.
    `approve` is required: with no way to ask, nothing is installed. Returns (text, is_error)."""
    if spec.installed():
        return f"{spec.name} is already installed ({spec.resolve()}).", False
    if approve is None:
        return "error: no way to ask the user for approval here, so nothing was installed.", True
    cmd = pick_recipe(spec)
    if cmd is None:
        why = "no install command is configured for it" if not spec.install_cmds else \
            "none of its package managers (" + ", ".join(sorted({c[0] for c in spec.install_cmds})) + ") is installed"
        return f"cannot install {spec.name} automatically: {why}. Do it by hand: {spec.install or 'see its docs'}", True
    shown = " ".join(shlex_quote(a) for a in cmd)
    if not approve(f"install the {spec.name} language server: {shown}"):
        return f"not installed: the user declined `{shown}`.", True
    env = {**os.environ, "PATH": os.pathsep.join([*extra_bin_dirs(), os.environ.get("PATH", "")])}
    try:
        proc = popen([find_program(cmd[0]) or cmd[0], *cmd[1:]], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                     stderr=subprocess.STDOUT, text=True, env=env, start_new_session=True)
    except OSError as e:
        return f"error: could not run {cmd[0]}: {e}", True
    chunks: list[str] = []
    reader = threading.Thread(target=lambda: chunks.extend(iter(proc.stdout.readline, "")), daemon=True)
    reader.start()
    end = time.monotonic() + timeout
    while proc.poll() is None:
        if (stop is not None and stop.is_set()) or time.monotonic() > end:
            try:
                if proc.pid <= 1:
                    raise ProcessLookupError
                os.killpg(proc.pid, 15)
            except (OSError, ProcessLookupError):
                proc.kill()
            reason = "cancelled" if stop is not None and stop.is_set() else f"timed out after {timeout:.0f}s"
            return f"install of {spec.name} {reason}; it was stopped.", True
        time.sleep(0.2)
    reader.join(2)
    tail = "".join(chunks)[-800:].strip()
    if proc.returncode != 0:
        return f"`{shown}` failed (exit {proc.returncode}):\n{tail}", True
    where = spec.resolve()
    if where is None:
        return (f"`{shown}` finished but {spec.command[0]} is still not found. It may have installed to a folder that "
                f"is not on PATH; add that folder to PATH and try again.\n{tail}"), True
    return f"installed {spec.name} ({where}). The lsp tool can use it now.", False


def shlex_quote(a: str) -> str:
    import shlex
    return shlex.quote(a)


def run_install(args: dict, cwd: str, approve, stop=None) -> tuple[str, bool]:
    reg = load_registry(cwd)
    name = str(args.get("server", "")).strip().lower()
    spec = reg.get(name) or next((s for s in reg.values() if name in (s.name.lower(), *[e.lstrip(".") for e in s.extensions])), None)
    if spec is None:
        return f"error: unknown server {name!r}. Known: {', '.join(sorted(reg))}", True
    return install_server(spec, approve, stop)
