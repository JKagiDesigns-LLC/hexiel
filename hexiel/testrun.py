"""Test runs hexiel recognises and shows progress for — any framework, any language.

A shell command that runs a test suite (pytest, jest, go test, cargo test, mvn test, rspec, dotnet test, ...)
is detected by its command line. While it runs, its output is read for progress: an explicit percentage or
"N/M" counter when the framework prints one, otherwise a count of finished tests/files measured against how
many the previous run of the same command had, otherwise the previous run's duration. Every view (terminal
footer and activity line, web console, phone app) draws the same bar from `snapshot()`, so the model never
has to poll a suite's output — the bash tool waits for it, or a background run wakes the model when it ends.
`[settings] test_commands = ["regex", ...]` adds project-specific runners."""

import hashlib
import json
import os
import re
import shlex
import threading
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# ---------------------------------------------------------------- detecting a test command

_WRAPPERS = {"time", "sudo", "nice", "env", "command", "exec", "xvfb-run", "caffeinate", "stdbuf", "ionice",
             "nohup", "timeout", "unbuffer", "script"}
_RUNNER_PREFIXES = [("uv", "run"), ("poetry", "run"), ("pipenv", "run"), ("pdm", "run"), ("hatch", "run"),
                    ("rye", "run"), ("bundle", "exec"), ("pnpm", "exec"), ("pnpm", "dlx"), ("yarn", "dlx"),
                    ("yarn", "exec"), ("npm", "exec"), ("npx",), ("bunx",), ("pnpx",), ("mise", "exec"),
                    ("python", "-m"), ("python3", "-m"), ("py", "-m")]
_BARE = {                       # first word -> framework label
    "pytest": "pytest", "py.test": "pytest", "tox": "tox", "nox": "nox", "behave": "behave",
    "nosetests": "nose", "trial": "trial", "robot": "robot", "unittest": "unittest", "doctest": "doctest",
    "jest": "jest", "vitest": "vitest", "mocha": "mocha", "ava": "ava", "tap": "tap", "jasmine": "jasmine",
    "karma": "karma", "wtr": "web-test-runner", "rspec": "rspec", "phpunit": "phpunit", "pest": "pest",
    "codecept": "codeception", "ctest": "ctest", "prove": "prove", "bats": "bats", "busted": "busted",
    "hardhat": "hardhat", "truffle": "truffle", "forge": "forge", "pytest-xdist": "pytest",
    "invoke-pester": "pester", "pester": "pester", "nunit3-console": "nunit", "vstest.console": "vstest",
    "xunit": "xunit", "kaocha": "kaocha", "playwright": "playwright", "cypress": "cypress",
    "testem": "testem", "qunit": "qunit", "uvu": "uvu", "tape": "tape", "lab": "lab",
}
_SUB = {                        # first word -> (subcommands that mean "run the tests"), label
    "go": ({"test"}, "go test"), "cargo": ({"test", "nextest", "t"}, "cargo test"),
    "dotnet": ({"test"}, "dotnet test"), "mix": ({"test"}, "mix test"), "swift": ({"test"}, "swift test"),
    "flutter": ({"test"}, "flutter test"), "dart": ({"test"}, "dart test"), "deno": ({"test"}, "deno test"),
    "bun": ({"test"}, "bun test"), "sbt": ({"test", "testonly", "testquick"}, "sbt test"),
    "lein": ({"test"}, "lein test"), "zig": ({"test", "build"}, "zig test"), "rake": ({"test", "spec"}, "rake test"),
    "xcodebuild": ({"test", "test-without-building"}, "xcodebuild test"),
}
_NODE_PM = {"npm", "yarn", "pnpm", "bun"}
_BUILD_TOOLS = {"mvn": ("maven", {"test", "verify", "integration-test", "surefire:test", "failsafe:integration-test"}),
                "mvnw": ("maven", {"test", "verify", "integration-test"}),
                "gradle": ("gradle", None), "gradlew": ("gradle", None), "make": ("make", {"test", "tests", "check", "unittest", "ci", "e2e", "smoke", "integration", "verify", "bdd"}),
                "ninja": ("ninja", {"test", "check"}), "just": ("just", {"test", "tests", "check", "ci"}),
                "task": ("task", {"test", "tests", "check", "ci"})}
_TEST_WORD = re.compile(r"(^|[:_-])(test|tests|spec|specs|e2e|ci|check|integration|unit|smoke|bdd|cucumber|qa)($|[:_-])")
# a script that is evidently a test run by where it lives or what it is called: e2e-bdd/run.sh, ./scripts/test.sh,
# run_tests.sh, bash qa/smoke.sh ... (any path component named like test/e2e/bdd/smoke/spec/integration/qa/ci)
_TEST_PATH_WORD = re.compile(r"^(.*[-_.])?(e2e|bdd|smoke|tests?|specs?|integration|qa|ci|acceptance|regression)([-_.].*)?$", re.I)
_INTERPRETERS = {"bash", "sh", "zsh", "dash", "ksh", "fish", "pwsh", "powershell", "node", "ruby", "perl", "php", "python", "python3", "py"}
_SCRIPT_EXT = re.compile(r"\.(sh|bash|zsh|ps1|py|rb|js|mjs|cjs|ts|pl|php|bat|cmd)$", re.I)


def _is_test_script(token: str) -> bool:
    if not token or token.startswith("-") or "=" in token.split("/")[0]:
        return False
    pathlike = "/" in token or token.startswith(".") or bool(_SCRIPT_EXT.search(token))
    if not pathlike:
        return False
    parts = [p for p in re.split(r"[\\/]+", token) if p and p not in (".", "..", "~")]
    return any(_TEST_PATH_WORD.match(re.sub(_SCRIPT_EXT, "", p)) for p in parts)


_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\n|\|)\s*")


def _segments(cmd: str):
    for seg in _SPLIT.split(cmd):
        seg = seg.strip()
        if seg:
            try:
                yield shlex.split(seg, posix=True)
            except ValueError:
                yield seg.split()


def _strip(argv: list[str]) -> list[str]:
    """Drop `FOO=1`, `sudo`, `uv run`, `npx`, `python -m` ... so the real program leads."""
    argv = list(argv)
    changed = True
    while argv and changed:
        changed = False
        while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
            argv.pop(0)
            changed = True
        if argv and Path(argv[0]).name in _WRAPPERS:
            argv.pop(0)
            if argv and argv[0].isdigit():            # `timeout 600 cmd`
                argv.pop(0)
            while argv and argv[0].startswith("-"):
                argv.pop(0)
            changed = True
        for prefix in _RUNNER_PREFIXES:
            if tuple(Path(a).name for a in argv[:len(prefix)]) == prefix:
                argv = argv[len(prefix):]
                while argv and argv[0].startswith("-") and prefix[0] in ("npx", "bunx", "pnpx"):
                    argv.pop(0)
                changed = True
                break
    return argv


_extra_cache: dict = {}


def _extra_patterns(patterns) -> list:
    key = tuple(patterns or ())
    if key not in _extra_cache:
        out = []
        for p in key:
            try:
                out.append(re.compile(str(p)))
            except re.error:
                pass
        _extra_cache[key] = out
    return _extra_cache[key]


def detect(cmd: str, extra=()) -> str:
    """The framework a command line runs ("pytest", "go test", ...) or "" when it is not a test run."""
    for rx in _extra_patterns(extra):
        if rx.search(cmd or ""):
            return "tests"
    for argv in _segments(cmd or ""):
        argv = _strip(argv)
        if not argv:
            continue
        prog = Path(argv[0]).name
        if prog.endswith(".exe"):
            prog = prog[:-4]
        rest = [a for a in argv[1:]]
        subs = [a for a in rest if not a.startswith("-")]
        if prog in _INTERPRETERS and subs:
            base = re.sub(r"\.(c|m)?js$|\.py$", "", Path(subs[0]).name)
            if base in _BARE:
                inner = detect(" ".join([base] + [shlex.quote(x) for x in rest[rest.index(subs[0]) + 1:]]))
                if inner:
                    return inner
        script = argv[0] if ("/" in argv[0] or argv[0].startswith(".")) and prog not in _INTERPRETERS else (
            subs[0] if prog in _INTERPRETERS and subs else "")
        if script and _is_test_script(script) and not (prog in ("python", "python3", "py") and rest[:1] == ["-m"]):
            return "tests"
        if prog in ("python", "python3", "py") and rest[:1] == ["-m"] and len(rest) > 1:
            prog = rest[1]                                  # `python -m pytest` when the prefix strip missed it
            rest, subs = rest[2:], [a for a in rest[2:] if not a.startswith("-")]
        if prog in _BARE:
            if prog in ("cypress",) and "run" not in subs:
                continue
            if prog in ("playwright",) and (not subs or subs[0] != "test"):
                continue
            if prog in ("forge", "hardhat", "truffle") and (not subs or subs[0] != "test"):
                continue
            return _BARE[prog]
        if prog in _SUB and subs:
            names, label = _SUB[prog]
            if prog == "zig" and not (subs[0] == "test" or (subs[0] == "build" and len(subs) > 1 and "test" in subs[1])):
                continue
            if subs[0] in names:
                return label
        if prog in _NODE_PM and subs:
            first = subs[0]
            if first in ("test", "t", "tst", "ci-test"):
                return f"{prog} test"
            if first in ("run", "run-script") and len(subs) > 1 and _TEST_WORD.search(subs[1]):
                return f"{prog} run {subs[1]}"
            if prog in ("yarn", "pnpm") and _TEST_WORD.search(first):
                return f"{prog} {first}"
        if prog in _BUILD_TOOLS:
            label, goals = _BUILD_TOOLS[prog]
            if goals is None:                                # gradle: any task with test/check in its name
                if any(re.search(r"(test|check|connected)", s, re.I) for s in subs):
                    return label + " test"
            elif any(s in goals or s.split(":")[-1] in goals for s in subs):
                return label + " test"
        if prog == "cmake" and "--target" in rest:
            idx = rest.index("--target")
            if idx + 1 < len(rest) and rest[idx + 1] in ("test", "check", "RUN_TESTS"):
                return "cmake test"
        if prog in ("ruby", "php", "node", "perl", "Rscript", "lua", "elixir") and rest:
            name = Path(rest[-1]).name if not rest[-1].startswith("-") else ""
            if re.match(r"^(test|spec|tests)[_.-].*|.*[_.-](test|spec)\.\w+$", name):
                return f"{prog} tests"
    return ""


# ---------------------------------------------------------------- reading progress out of output

# marker kinds that count a finished unit — the first kind a run shows is kept, so one stream never double counts
_UNITS = [
    ("cargo", re.compile(r"^test .+ \.\.\. (ok|FAILED|ignored|bench)")),
    ("tap", re.compile(r"^\s*(not )?ok \d+")),
    ("verbose", re.compile(r"\b(PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)\b(?!\s*:?\s*\d+ )")),
    ("scenario", re.compile(r"^\s*Scenario( Outline)?:")),                                         # Cucumber / Behave / Gherkin
    ("jestfile", re.compile(r"^\s*(PASS|FAIL)\s+\S")),
    ("tick", re.compile(r"^\s*(✓|✔|√|✗|✘|×|✕)")),
    ("gotest", re.compile(r"^(--- (PASS|FAIL|SKIP):|ok\s+\S+\s|FAIL\s+\S+\s+[\d.]+s)")),
    ("dotnet", re.compile(r"^\s*(Passed|Failed|Skipped)\s+\S")),
    ("maven", re.compile(r"Tests run: \d+, Failures")),
    ("gradle", re.compile(r"^> Task \S*[Tt]est\S*( |$)")),
    ("dots", re.compile(r"^[.FEsSxX*]{2,}\s*(\[\s*\d+%\])?\s*$")),
    ("dots-pct", re.compile(r"^\S+\.(py|rb|js|ts)\s+[.FEsSxX]+")),
]
_FAILED = re.compile(r"\b(FAILED|FAIL|FAILURE|not ok|ERROR)\b|^\s*(✗|✘|×|✕)")
_PCT = re.compile(r"\[\s*(\d{1,3})%\]")
_FRACTION = re.compile(r"(?:^|\s)(\d+)/(\d+)\s+(?:Test|test)\b|^\s*Test\s+#?(\d+)\s*/\s*(\d+)\b|^\[(\d+)/(\d+)\]")
_CARGO_TOTAL = re.compile(r"^running (\d+) tests?")
_ANNOUNCED = re.compile(r"^\s*Running (\d+) tests?( using|\b)|^\s*(\d+) (scenarios?|tests?) to run", re.I)   # Playwright, Cypress-style plans
_TAP_PLAN = re.compile(r"^1\.\.(\d+)\s*$")
_SUMMARY = re.compile(r"(\d+) (passed|failed|failures?|errors?|skipped|tests? run)|Tests:\s+.*?(\d+) total|"
                      r"Ran (\d+) tests?|\bok\s+\S+\s+[\d.]+s", re.I)


def history_file() -> Path:
    from .config import home_data_dir
    return home_data_dir() / "test_history.json"


def _hkey(cwd: str, cmd: str) -> str:
    norm = " ".join((cmd or "").split())
    return hashlib.sha256(f"{cwd}\0{norm}".encode()).hexdigest()[:12]


def _load_history() -> dict:
    try:
        return json.loads(history_file().read_text())
    except (OSError, ValueError):
        return {}


@dataclass
class Run:
    id: str
    label: str
    cmd: str
    cwd: str
    started: float = field(default_factory=time.time)
    ended: float | None = None
    code: int | None = None
    units: int = 0                    # tests (or files/classes) finished so far
    failed: int = 0
    total: int | None = None          # known total from the output
    kind: str = ""                    # which marker is being counted
    pct_out: float | None = None      # percentage the framework itself printed (0..1)
    prev: dict = field(default_factory=dict)          # the last finished run of this command: duration, units, kind
    last_line: str = ""
    is_tests: bool = True             # False for a long command that has not (yet) shown itself to be a test run
    _dirty: bool = False
    _notified: float = 0.0
    _tail: list = field(default_factory=list)

    # -- feeding ---------------------------------------------------
    def feed(self, raw: str) -> None:
        line = _ANSI.sub("", raw).rstrip("\r\n")
        line = line.split("\r")[-1]
        if not line.strip():
            return
        self.last_line = line.strip()[:120]
        self._tail = (self._tail + [line])[-40:]
        m = _PCT.search(line)
        if m:
            self.pct_out = min(int(m.group(1)), 100) / 100
        m = _FRACTION.search(line)
        if m:
            nums = [int(g) for g in m.groups() if g is not None]
            if len(nums) >= 2 and nums[1] > 0:
                self.units, self.total = min(nums[0], nums[1]), nums[1]
                self.kind = self.kind or "fraction"
        m = _CARGO_TOTAL.match(line)
        if m:
            self.total = (self.total or 0) + int(m.group(1)) if self.kind == "cargo" else int(m.group(1))
            self.kind = "cargo"
        m = _ANNOUNCED.match(line)
        if m and not self.total:
            self.total = int(m.group(1) or m.group(3))
        m = _TAP_PLAN.match(line)
        if m:
            self.total, self.kind = int(m.group(1)), "tap"
        for kind, rx in _UNITS:
            if (not self.kind or self.kind == kind) and rx.search(line):
                self.kind = kind
                n = len(re.findall(r"[.FEsSxX*]", line.split("[")[0])) if kind == "dots" else 1
                self.units += n
                break
        if _FAILED.search(line) and (self.kind in ("cargo", "tap", "verbose", "tick", "dotnet", "gotest", "jestfile", "")
                                      or "F" in line[:12]):
            self.failed += 1 if self.kind != "dots" else line.count("F") + line.count("E")
        self._dirty = True

    def finish(self, code: int | None) -> None:
        self.ended, self.code = time.time(), code
        self._dirty = True
        if code == 0 or (not self.prev and (code or 0) >= 0):          # a killed run's duration says nothing
            h = _load_history()
            h[_hkey(self.cwd, self.cmd)] = {"duration": round(self.ended - self.started, 1), "units": self.units,
                                            "kind": self.kind, "at": int(self.ended), "tests": self.is_tests,
                                            "label": self.label if self.is_tests else ""}
            if len(h) > 400:
                for k in sorted(h, key=lambda k: h[k].get("at", 0))[:len(h) - 400]:
                    h.pop(k, None)
            try:
                history_file().parent.mkdir(parents=True, exist_ok=True)
                history_file().write_text(json.dumps(h))
            except OSError:
                pass

    # -- reading ---------------------------------------------------
    def elapsed(self) -> float:
        return (self.ended or time.time()) - self.started

    def fraction(self) -> tuple[float | None, str]:
        """(0..1 or None when unknowable, how it was worked out)"""
        if self.ended is not None:
            return 1.0, "done"
        if self.pct_out is not None:
            return min(self.pct_out, 0.99), "output"
        if self.total and self.units:
            return min(self.units / self.total, 0.99), "output"
        prev_units = self.prev.get("units") or 0
        if self.units and prev_units and self.prev.get("kind") == self.kind:
            return min(self.units / max(prev_units, self.units + 1), 0.99), "history"
        duration = self.prev.get("duration") or 0
        if duration > 2:
            return min(self.elapsed() / duration, 0.95), "time"
        return None, "unknown"

    def snapshot(self) -> dict:
        frac, how = self.fraction()
        eta = None
        if frac and 0 < frac < 1 and self.elapsed() > 3:
            eta = max(self.elapsed() * (1 - frac) / frac, 0)
        elif how == "time":
            eta = max((self.prev.get("duration") or 0) - self.elapsed(), 0)
        return {"id": self.id, "label": self.label, "kind": "tests" if self.is_tests else "command", "cmd": self.cmd[:200], "started": self.started,
                "elapsed": round(self.elapsed(), 1), "pct": None if frac is None else round(frac, 4), "how": how,
                "done": self.units, "total": self.total or (self.prev.get("units") or None
                                                           if how == "history" else None),
                "failed": self.failed, "eta": None if eta is None else round(eta, 1),
                "est": self.prev.get("duration") or None, "finished": self.ended is not None, "code": self.code,
                "last": self.last_line}

    def tail(self, n: int = 12) -> str:
        return "\n".join(self._tail[-n:])


# ---------------------------------------------------------------- the registry views read from

RUNS: dict[str, Run] = {}
_lock = threading.Lock()
_listeners: list = []                 # weak refs to callables (sessions) told when progress moves
KEEP_FINISHED = 20


def known_test(cwd: str, cmd: str) -> str:
    """The label of a command hexiel has seen running tests before ("" when unknown) — learned by watch()."""
    rec = _load_history().get(_hkey(cwd, cmd), {})
    return (rec.get("label") or "tests") if rec.get("tests") else ""


# ---------------------------------------------------------------- watching a command that is not obviously a test

PROMOTE_AFTER = 8.0               # seconds a foreground command runs before it earns a progress bar
_CARRIERS = {"docker", "podman", "docker-compose", "kubectl", "ssh", "nsenter", "bash", "sh", "zsh", "env", "sudo",
             "timeout", "nohup", "xvfb-run", "uv", "poetry", "npx", "pnpm", "yarn", "npm", "make", "just", "task",
             "flock", "stdbuf", "script", "time", "nice"}


def loose_detect(cmdline: str) -> str:
    """detect(), but also finds the runner behind a wrapper (docker compose run ... playwright test, bash -c ...,
    java -cp ... surefire) in a running process's command line."""
    label = detect(cmdline)
    if label:
        return label
    try:
        tokens = shlex.split(cmdline)
    except ValueError:
        tokens = cmdline.split()
    if not tokens:
        return ""
    prog = Path(tokens[0]).name
    if prog in _CARRIERS:
        for i in range(1, len(tokens)):
            label = detect(" ".join(shlex.quote(t) for t in tokens[i:]))
            if label:
                return label
    if prog == "java" and re.search(r"surefire|failsafe|junit|testng|cucumber", cmdline, re.I):
        return "junit"
    if prog == "dotnet" and re.search(r"vstest|testhost", cmdline):
        return "dotnet test"
    return ""


def _descendants(root: int) -> list[str]:
    """Command lines of every process below `root` (Linux and macOS: via ps; elsewhere nothing)."""
    try:
        import subprocess
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,args="], capture_output=True, text=True, timeout=3).stdout
    except Exception:                                         # noqa: BLE001 - no ps (Windows): no tree to read
        return []
    kids: dict[int, list[int]] = {}
    cmds: dict[int, str] = {}
    for ln in out.splitlines():
        parts = ln.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            kids.setdefault(int(parts[1]), []).append(int(parts[0]))
            cmds[int(parts[0])] = parts[2]
    found, queue = [], list(kids.get(root, []))
    while queue and len(found) < 400:
        pid = queue.pop()
        found.append(cmds.get(pid, ""))
        queue.extend(kids.get(pid, []))
    return found


def watch(proc, cmd: str, cwd: str) -> None:
    """Start watching a foreground command: once it has run PROMOTE_AFTER seconds it gets a progress bar (from the
    previous run of the same command, else a sweeping one), and the processes it started are read every second —
    the moment one of them is a test runner the bar becomes a test run, and that is remembered for next time."""
    threading.Thread(target=_watch, args=(proc, cmd, cwd), daemon=True, name="hexiel-watch").start()


def _watch(proc, cmd: str, cwd: str) -> None:
    began = time.time()
    while proc.poll() is None and time.time() - began < PROMOTE_AFTER:
        time.sleep(0.25)
    if proc.poll() is not None:
        return
    run = start(cmd, cwd, "running")
    run.is_tests, run.started = False, began
    try:
        while proc.poll() is None:
            if not run.is_tests:
                for child in _descendants(proc.pid):
                    label = loose_detect(child)
                    if label:
                        run.label, run.is_tests = label, True
                        break
            notify(run, force=True)
            time.sleep(1.0)
    finally:
        run.finish(proc.returncode if proc.returncode is not None else -1)
        notify(run, force=True)


def start(cmd: str, cwd: str, label: str) -> Run:
    run = Run(id=f"t{int(time.time() * 1000) % 10_000_000:x}", label=label, cmd=cmd, cwd=cwd)
    run.prev = _load_history().get(_hkey(cwd, cmd), {})
    with _lock:
        RUNS[run.id] = run
        done = [r for r in RUNS.values() if r.ended is not None]
        for old in sorted(done, key=lambda r: r.ended)[:max(0, len(done) - KEEP_FINISHED)]:
            RUNS.pop(old.id, None)
    notify(run, force=True)
    return run


def active() -> list[Run]:
    with _lock:
        return [r for r in RUNS.values() if r.ended is None]


def snapshots() -> list[dict]:
    return [r.snapshot() for r in active()]


def listen(callback) -> None:
    try:
        _listeners.append(weakref.WeakMethod(callback))
    except TypeError:
        _listeners.append(lambda cb=callback: cb)


def notify(run: Run, force: bool = False) -> None:
    now = time.time()
    if not force and now - run._notified < 0.5:
        return
    run._notified, run._dirty = now, False
    for ref in list(_listeners):
        cb = ref()
        if cb is None:
            _listeners.remove(ref)
            continue
        try:
            cb(run.snapshot())
        except Exception:                                   # noqa: BLE001 - a view must never break a test run
            pass


# ---------------------------------------------------------------- drawing it

def fmt_clock(seconds: float | None) -> str:
    seconds = int(max(0, round(seconds or 0)))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def bar(pct: float | None, width: int = 16, tick: int = 0) -> str:
    if pct is None:                                         # unknown: a block sweeping back and forth
        span = max(width - 4, 1)
        pos = tick % (2 * span)
        pos = pos if pos <= span else 2 * span - pos
        return "░" * pos + "████" + "░" * (width - 4 - pos)
    filled = int(round(max(0.0, min(pct, 1.0)) * width))
    return "█" * filled + "░" * (width - filled)


def line(snap: dict, width: int = 16, tick: int = 0) -> str:
    """`pytest ███████░░░░░░░░░ 45% · 1:23 · ~1:40 left · 2 failed`"""
    bits = [f"{snap['label']} {bar(snap['pct'], width, tick)}"]
    if snap["pct"] is not None:
        bits[0] += f" {int(snap['pct'] * 100)}%"
    if snap.get("done") and snap.get("total"):
        bits.append(f"{snap['done']}/{snap['total']}")
    bits.append(fmt_clock(snap["elapsed"]))
    if snap.get("eta") is not None and snap["pct"] is not None:
        bits.append(f"~{fmt_clock(snap['eta'])} left")
    if snap.get("failed"):
        bits.append(f"{snap['failed']} failed")
    return " · ".join(bits)


def summary(tick: int = 0, width: int = 16) -> str:
    """One footer line for the run in progress (+N when several), empty when nothing is running."""
    runs = sorted(active(), key=lambda r: r.started)
    if not runs:
        return ""
    return ("🧪 " if runs[0].is_tests else "⏳ ") + line(runs[0].snapshot(), width, tick) + (f" (+{len(runs) - 1})" if len(runs) > 1 else "")


def progress_note(run: Run) -> str:
    """What the model is told if it asks a running suite for its output: where it is, and not to poll."""
    s = run.snapshot()
    return (f"[test run in progress — {line(s)}. The user sees this progress bar; do NOT poll. "
            "End your turn or do other work: you are woken automatically when it finishes.]")
