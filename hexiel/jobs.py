"""Scheduled jobs: "every Monday at 8am, summarise sales.csv" — run headless on a
schedule, results delivered to an inbox with a desktop notification.

One OS entry runs `hexiel jobs tick` every minute (systemd user timer or cron on
Linux, launchd on macOS, Task Scheduler on Windows); the tick runs whichever
jobs are due. Jobs live in ~/.local/share/hexiel/jobs.json, results in
~/.local/share/hexiel/inbox/. Missed runs (machine asleep) run once, not N times.
Jobs are read-only by default: they can read, search and research, but writes
and commands are refused unless the job was created with access="full"."""

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .config import home_data_dir

NAMES = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
         "sep": 9, "oct": 10, "nov": 11, "dec": 12,
         "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@weekly": "0 0 * * 0",
           "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *"}


def jobs_file() -> Path:
    return home_data_dir() / "jobs.json"


def inbox_dir() -> Path:
    return home_data_dir() / "inbox"


# ---------------------------------------------------------------- cron

def _field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.lower().split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a_s, b_s = part.split("-", 1)
            a, b = int(NAMES.get(a_s, a_s)), int(NAMES.get(b_s, b_s))
        else:
            a = int(NAMES.get(part, part))
            b = hi if step > 1 else a
        out.update(range(a, b + 1, step))
    return out


def parse_cron(expr: str) -> tuple:
    expr = ALIASES.get(expr.strip(), expr.strip())
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron needs 5 fields (minute hour day month weekday), got {expr!r}")
    minute, hour, dom, month, dow = parts
    dows = _field(dow, 0, 7)
    if 7 in dows:
        dows = (dows - {7}) | {0}
    return (_field(minute, 0, 59), _field(hour, 0, 23), _field(dom, 1, 31), _field(month, 1, 12),
            dows, dom != "*", dow != "*")


def cron_matches(expr: str, when: datetime.datetime) -> bool:
    minutes, hours, doms, months, dows, dom_set, dow_set = parse_cron(expr)
    if when.minute not in minutes or when.hour not in hours or when.month not in months:
        return False
    dow = (when.weekday() + 1) % 7          # cron: Sunday = 0
    day_ok = when.day in doms, dow in dows
    if dom_set and dow_set:
        return day_ok[0] or day_ok[1]       # cron's "either" rule
    return day_ok[0] and day_ok[1]


def last_due(expr: str, now: datetime.datetime, lookback_hours: int = 24 * 8) -> datetime.datetime | None:
    """Most recent minute <= now matching expr (within lookback)."""
    t = now.replace(second=0, microsecond=0)
    for _ in range(lookback_hours * 60):
        if cron_matches(expr, t):
            return t
        t -= datetime.timedelta(minutes=1)
    return None


# ---------------------------------------------------------------- store

def load() -> list[dict]:
    try:
        return json.loads(jobs_file().read_text())
    except OSError:
        return []
    except ValueError:                      # a damaged file: keep it for recovery instead of overwriting it with []
        try:
            shutil.copy2(jobs_file(), jobs_file().with_name(f"jobs.json.corrupt-{int(time.time())}"))
        except OSError:
            pass
        return []


def save(jobs: list[dict]) -> None:
    jobs_file().parent.mkdir(parents=True, exist_ok=True)
    tmp = jobs_file().with_name(f".jobs.json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(jobs, indent=2))
    os.replace(tmp, jobs_file())            # whole-file swap: a crash never leaves half a job list


def _update_job(job_id: str, **fields) -> None:
    """Change fields of ONE job in the file as it is now. A tick can run for many minutes, and the user may
    add or remove jobs meanwhile: saving the list loaded at the start would undo that."""
    current = load()
    for j in current:
        if j.get("id") == job_id:
            j.update(fields)
            save(current)
            return


def add(name: str, schedule: str, prompt: str, cwd: str, model: str = "", access: str = "readonly") -> dict:
    parse_cron(schedule)                    # validate
    job = {"id": uuid.uuid4().hex[:8], "name": name or prompt[:40], "schedule": schedule.strip(),
           "prompt": prompt, "cwd": cwd, "model": model, "access": access, "enabled": True,
           "created": time.time(), "last_run": None, "last_status": None}
    jobs = load()
    jobs.append(job)
    save(jobs)
    return job


def remove(job_id: str) -> bool:
    jobs = load()
    keep = [j for j in jobs if j["id"] != job_id and j["name"] != job_id]
    save(keep)
    return len(keep) != len(jobs)


# ---------------------------------------------------------------- running

JOB_STALL_SECONDS = 600       # no model output and no tool activity for this long = a hung model server
JOB_POLL_SECONDS = 5
JOB_SWITCH_SECONDS = 240      # with a fallback model available, move on after this much silence
INCOMPLETE_MARK = "COULD NOT COMPLETE:"   # a report that starts with this is a failure, not a success
JOB_MAX_SECONDS = 25 * 60     # hard cap on one unattended run (the relay gives up on a silent device at 35 min)


def _gist(arguments) -> str:
    """One short line describing a tool call's arguments."""
    if not isinstance(arguments, dict):
        return ""
    for key in ("url", "query", "command", "path", "pattern", "prompt"):
        if arguments.get(key):
            return str(arguments[key]).replace("\n", " ")[:140]
    return json.dumps(arguments, default=str)[:140]


class _Progress:
    """Turns a run's events into a console-style log and hands it to `send` in small, frequent batches,
    so the account page can show what the machine is doing as it happens.

    Line markers (the page colours them): `▸` a tool call, `✓`/`✗` its result, `~` the model's thinking,
    `!` a problem (unreachable, rate limited, switching models), `#` context (which model is running);
    anything else is the model's own words."""

    CAP = 64_000                      # most we send for one run
    PREVIEW_LINES, PREVIEW_CHARS = 6, 500

    def __init__(self, send, every: float = 0.4) -> None:
        self.send, self.every = send, every
        self.buf = ""
        self.sent = 0
        self.t0 = time.time()
        self.last = 0.0
        self.thinking = ""            # the reasoning being streamed right now
        self.mid_line = False         # the model's words are part-way through a line
        self.lock = threading.RLock()
        self.timer: threading.Timer | None = None

    def _add(self, text: str) -> None:
        with self.lock:
            self.buf += text
            if self.timer is None:    # whatever arrives is sent within `every` seconds, even if the model then goes quiet
                self.timer = threading.Timer(max(0.0, self.every - (time.time() - self.last)), self.flush)
                self.timer.daemon = True
                self.timer.start()

    def _newline(self) -> None:
        if self.mid_line:
            self._add("\n")
            self.mid_line = False

    def _stamp(self) -> str:
        return f"[{int(time.time() - self.t0)}s]"

    def context(self, text: str) -> None:
        self._newline()
        self._add(f"# {text}\n")
        self.flush()

    def problem(self, text: str) -> None:
        self._newline()
        self._add(f"! {text}\n")
        self.flush()

    def event(self, kind: str, args: tuple) -> None:
        if kind == "thinking":
            if not self.thinking:
                self._newline()
                self._add(f"~ thinking…\n")
            self.thinking += str(args[0]) if args else ""
            return self.maybe()
        if self.thinking:                          # the thought ended: keep a short trace of it
            snippet = " ".join(self.thinking.split())[:300]
            self.thinking = ""
            if snippet:
                self._add(f"~ {snippet}\n")
        if kind == "text" and args:
            self._add(str(args[0]))
            self.mid_line = not str(args[0]).endswith("\n")
        elif kind == "tool_call" and args:
            self._newline()
            self._add(f"▸ {self._stamp()} {args[0].get('name', '?')} {_gist(args[0].get('arguments'))}\n")
        elif kind == "tool_result" and len(args) >= 3:
            self._newline()
            body = str(args[1]).strip().splitlines()
            preview = "\n".join("    " + line[:160] for line in body[: self.PREVIEW_LINES])[: self.PREVIEW_CHARS]
            took = f" {args[3]:.1f}s" if len(args) > 3 and isinstance(args[3], (int, float)) else ""
            self._add(f"  {'✗' if args[2] else '✓'}{took}\n{preview}\n" if preview else f"  {'✗' if args[2] else '✓'}{took}\n")
        elif kind == "note" and args:
            self.context(str(args[0]))
        elif kind == "model_unreachable" and len(args) >= 2:
            self.problem(f"model server not answering ({str(args[1])[:100]}) — retrying")
        elif kind == "rate_limited" and len(args) >= 2:
            self.problem(f"rate limited — waiting ({str(args[1])[:100]})")
        self.maybe()

    def maybe(self) -> None:
        if time.time() - self.last >= self.every:
            self.flush()

    def flush(self) -> None:
        with self.lock:
            timer, self.timer = self.timer, None
            if timer is not None:
                timer.cancel()
            if not self.buf or self.sent >= self.CAP:
                self.buf = ""
                return
            chunk, self.buf = self.buf[: self.CAP - self.sent], ""
            self.sent += len(chunk)
            self.last = time.time()
        try:
            self.send(chunk)
        except Exception:             # noqa: BLE001 - progress is a courtesy; it must never break the run
            pass


class _Watch:
    """What happened during a headless run, so a failure can say why instead of 'no answer'."""

    def __init__(self, progress: "_Progress | None" = None) -> None:
        self.progress = progress
        self.last = time.time()
        self.tools: list[str] = []
        self.tool_errors: list[str] = []
        self.problems: list[str] = []
        self.finished = False             # the turn ended normally (not an error, not aborted)

    def on_event(self, kind: str, *args, **kwargs) -> None:
        self.last = time.time()
        if self.progress is not None:
            self.progress.event(kind, args)
        if kind == "tool_call" and args:
            self.tools.append(str(args[0].get("name", "?")))
        elif kind == "tool_result" and len(args) >= 3 and args[2]:
            self.tool_errors.append(f"{args[0].get('name', '?')}: {str(args[1])[:240]}")
        elif kind == "turn_end":
            if args and isinstance(args[0], dict) and args[0].get("error"):
                self.problems.append(str(args[0]["error"]))
            elif not kwargs.get("aborted"):
                self.finished = True
        elif kind == "provider_error" and args:
            self.problems.append(str(args[0]))
        elif kind == "model_unreachable" and len(args) >= 2:
            self.problems.append(f"the model server was unreachable or hung ({args[1]}); hexiel retried")
        elif kind == "rate_limited" and len(args) >= 2:
            self.problems.append(f"rate limited ({args[1]}); hexiel waited")


def _report(headline: str, watch: _Watch, model: str, started: float, partial: str = "") -> str:
    lines = [headline, ""]
    lines.append(f"Model: {model} · ran {int(time.time() - started)}s · {len(watch.tools)} tool call(s)"
                 + (f" ({', '.join(dict.fromkeys(watch.tools))})" if watch.tools else ""))
    for problem in dict.fromkeys(watch.problems):
        lines.append(f"Problem: {problem}")
    for err in watch.tool_errors[-3:]:
        lines.append(f"Tool error: {err}")
    if partial:
        lines += ["", "Last thing the model said (not a finished report):", partial[:1500]]
    return "\n".join(lines)


def _run_once(job: dict, model: str | None, progress, stall: float) -> tuple[bool, str, bool]:
    """One attempt on one model: (ok, text, retry). retry = another model might do better (this one stalled,
    crashed, could not start or answered nothing) — as opposed to a task that is simply not doable."""
    started = time.time()
    live = _Progress(progress) if progress else None
    watch = _Watch(live)
    name = model or "default model"
    try:
        from .main import build_session
        session = build_session(job["cwd"], model)
    except (Exception, SystemExit) as exc:        # a bad config or unknown model profile must not kill the caller
        return False, f"The run could not start: {exc}", True
    name = f"{model or 'default'} ({session.model_config.model})"
    if watch.progress is not None:
        watch.progress.context(f"running on {name}")
    full = job.get("access") == "full"
    session.on_approve = lambda tool, args: "yes" if full else "no"
    session.on_event = watch.on_event
    failure: list[BaseException] = []

    def work() -> None:
        try:
            session.run_turn(f"[scheduled job: {job['name']}] {job['prompt']}\n\n(You are running unattended "
                             "on a schedule: do the task and end with a clear, self-contained report. "
                             f"If you could not actually complete the task (a page was blocked, a value could not "
                             f"be found), start the report with '{INCOMPLETE_MARK}' and say why. "
                             + ("" if full else "You have read-only access: do not try to change files or "
                                "run commands that modify anything.") + ")")
        except BaseException as exc:  # noqa: BLE001 - reported below, never raised into the tunnel
            failure.append(exc)

    thread = threading.Thread(target=work, daemon=True, name="hexiel-job")
    thread.start()
    cut = ""
    while thread.is_alive():
        thread.join(JOB_POLL_SECONDS)
        if live:
            live.flush()
        idle, total = time.time() - watch.last, time.time() - started
        if thread.is_alive() and idle > stall:
            quiet = f"{int(idle)} seconds" if idle < 120 else f"{int(idle // 60)} minutes"
            cut = (f"Stopped: nothing happened for {quiet}. The model server is probably hung or "
                   "far too slow — check it, or pick a different model for this automation.")
        elif thread.is_alive() and total > JOB_MAX_SECONDS:
            cut = f"Stopped: the run took longer than {JOB_MAX_SECONDS // 60} minutes."
        if cut:
            session.stop()                        # also aborts a request blocked on a silent server
            thread.join(30)
            break
    if live:
        live.flush()
    last = next((m for m in reversed(session.messages) if m.get("role") == "assistant"), {})
    answer = (last.get("content") or "").strip()
    if cut:
        return False, _report(cut, watch, name, started, answer), cut.startswith("Stopped: nothing happened")
    if failure:
        exc = failure[0]
        return False, _report(f"The run crashed: {type(exc).__name__}: {exc}", watch, name, started, answer), True
    if watch.finished and answer and not last.get("tool_calls"):
        if INCOMPLETE_MARK in answer[:200].upper():
            return False, _report("The task could not be completed. The model's report:", watch, name, started, answer), False
        return True, answer, False
    if watch.finished:
        return False, _report("The run finished but the model returned nothing — no report was produced.",
                              watch, name, started), True
    return False, _report("The run did not complete.", watch, name, started, answer), True


def _fallbacks(job: dict, tried: str | None) -> list[str]:
    """Other model profiles to try, best first: the ones you set as fallbacks, then any other profile.
    Two at most, so an unattended job can't wander through every (possibly paid) model you have."""
    try:
        from .config import load_config
        cfg = load_config()
    except (Exception, SystemExit):
        return []
    s = cfg.settings
    names = [s.fallback_profile, s.worker_fallback_profile, s.planner_profile, *cfg.models]
    skip = {tried or cfg.default_model, ""}
    out: list[str] = []
    for name in names:
        if name in cfg.models and name not in skip and name not in out:
            key = getattr(cfg.models[name], "api_key_env", "")
            if key and not os.environ.get(key):          # a model that cannot even start is no fallback
                continue
            out.append(name)
    return out[:2]


def run_job(job: dict, progress=None) -> tuple[bool, str]:
    """Run one job headless; returns (ok, text). ok is True only for a finished report. Anything else —
    a crash, a hung or unreachable model, an empty reply — comes back as ok=False with the reason, never as
    a blank or a generic 'no answer'. `progress(text)`, if given, receives the activity log as it happens.

    When a model stalls (typically because another task is using it), the run moves to another model
    instead of waiting behind it, and the result says which model actually did the work."""
    first = job.get("model") or None
    chain: list[str | None] = [first]
    extra = _fallbacks(job, first)
    chain += extra
    attempts: list[str] = []
    for i, model in enumerate(chain):
        last_try = i == len(chain) - 1
        # with somewhere to go, don't sit behind a busy model for the full stall limit
        stall = JOB_STALL_SECONDS if last_try else min(JOB_STALL_SECONDS, JOB_SWITCH_SECONDS)
        ok, text, retry = _run_once(job, model, progress, stall)
        label = model or "the default model"
        if ok or not retry or last_try:
            if attempts:
                tried = "; ".join(attempts)
                text = (f"{text}\n\n(Ran on {label} after earlier attempts failed: {tried})" if ok
                        else f"{text}\n\nEarlier attempts: {tried}")
            return ok, text
        reason = text.strip().splitlines()[0][:160]
        attempts.append(f"{label}: {reason}")
        if progress:
            try:
                progress(f"\n! {label} did not work out ({reason}) — switching to {chain[i + 1]}\n")
            except Exception:                    # noqa: BLE001 - progress is a courtesy
                pass
    return False, "The run could not be attempted."


def deliver(job: dict, ok: bool, answer: str) -> Path:
    inbox_dir().mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    path = inbox_dir() / f"{stamp}-{re.sub(r'[^a-z0-9]+', '-', job['name'].lower())[:40]}.md"
    path.write_text(f"# {job['name']}\n\n_{datetime.datetime.now():%Y-%m-%d %H:%M} · "
                    f"{'ok' if ok else 'FAILED'} · {job['cwd']}_\n\n{answer}\n")
    _notify(f"hexiel job: {job['name']}" + ("" if ok else " — FAILED"), answer[:180])
    return path


def osascript_notify_argv(title: str, body: str) -> list[str]:
    """A macOS notification whose text travels as arguments, never spliced into the script: model output in
    the body (or a job name in the title) cannot break out of the string and run AppleScript."""
    return ["osascript", "-e", "on run argv", "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
            "-e", "end run", body, title]


def _notify(title: str, body: str) -> None:
    try:
        if shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-a", "hexiel", title, body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif shutil.which("osascript"):
            subprocess.Popen(osascript_notify_argv(title, body[:150]),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def tick(now: datetime.datetime | None = None, runner=run_job) -> list[str]:
    """Run every enabled job that has come due since it last ran. Lock-protected."""
    now = now or datetime.datetime.now()
    lock = home_data_dir() / "jobs.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        if time.time() - lock.stat().st_mtime < 6 * 3600:
            return []                       # another tick is still running
        lock.unlink(missing_ok=True)        # stale lock from a crash
        return tick(now, runner)
    ran = []
    try:
        jobs = load()
        for job in jobs:
            if not job.get("enabled", True):
                continue
            due = last_due(job["schedule"], now)
            last = datetime.datetime.fromtimestamp(job["last_run"]) if job.get("last_run") else \
                datetime.datetime.fromtimestamp(job["created"])
            if due is None or due <= last:
                continue
            job["last_run"] = now.timestamp()       # mark first: a crash must not loop
            _update_job(job["id"], last_run=job["last_run"])
            try:
                ok, answer = runner(job)
            except Exception as exc:  # noqa: BLE001 - one failing job must not stop the others
                ok, answer = False, f"the job crashed: {exc!r}"
            job["last_status"] = "ok" if ok else "failed"
            _update_job(job["id"], last_status=job["last_status"])
            deliver(job, ok, answer)
            ran.append(job["id"])
    finally:
        lock.unlink(missing_ok=True)
    return ran


# ---------------------------------------------------------------- OS integration

def tick_command() -> list[str]:
    return [sys.executable, "-m", "hexiel", "jobs", "tick"]


def install() -> str:
    """Register the every-minute tick with the OS scheduler (idempotent)."""
    cmd = tick_command()
    if os.name == "nt":
        tr = " ".join(f'"{c}"' if " " in c else c for c in cmd)
        r = subprocess.run(["schtasks", "/create", "/f", "/sc", "minute", "/mo", "1",
                            "/tn", "hexiel-jobs-tick", "/tr", tr], capture_output=True, text=True)
        return "Windows Task Scheduler: hexiel-jobs-tick (every minute)" if r.returncode == 0 else r.stderr
    if sys.platform == "darwin":
        plist = Path.home() / "Library/LaunchAgents/tech.hexiel.jobs.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        args = "".join(f"<string>{c}</string>" for c in cmd)
        plist.write_text(f'<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
                         f'<key>Label</key><string>tech.hexiel.jobs</string><key>ProgramArguments</key>'
                         f'<array>{args}</array><key>StartInterval</key><integer>60</integer></dict></plist>\n')
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        subprocess.run(["launchctl", "load", str(plist)], capture_output=True)
        return f"launchd agent {plist}"
    unit_dir = Path.home() / ".config/systemd/user"
    if shutil.which("systemctl") and subprocess.run(["systemctl", "--user", "is-system-running"],
                                                   capture_output=True).returncode in (0, 1):
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / "hexiel-jobs.service").write_text(
            "[Unit]\nDescription=hexiel scheduled jobs tick\n\n[Service]\nType=oneshot\n"
            f"ExecStart={' '.join(cmd)}\n")
        (unit_dir / "hexiel-jobs.timer").write_text(
            "[Unit]\nDescription=Run hexiel scheduled jobs every minute\n\n[Timer]\n"
            "OnCalendar=*-*-* *:*:00\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        r = subprocess.run(["systemctl", "--user", "enable", "--now", "hexiel-jobs.timer"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return "systemd user timer hexiel-jobs.timer (every minute)"
    if not shutil.which("crontab"):
        return ("no scheduler found: install cron (e.g. `sudo pacman -S cronie`) or enable "
                "systemd user services, then run `hexiel jobs install`")
    line = f"* * * * * {' '.join(cmd)} >/dev/null 2>&1  # hexiel-jobs"
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    if "# hexiel-jobs" not in current:
        subprocess.run(["crontab", "-"], input=current.rstrip("\n") + ("\n" if current else "") + line + "\n",
                       text=True, capture_output=True)
    return "crontab entry (every minute)"


def installed() -> bool:
    if os.name == "nt":
        return subprocess.run(["schtasks", "/query", "/tn", "hexiel-jobs-tick"], capture_output=True).returncode == 0
    if sys.platform == "darwin":
        return (Path.home() / "Library/LaunchAgents/tech.hexiel.jobs.plist").exists()
    if (Path.home() / ".config/systemd/user/hexiel-jobs.timer").exists():
        return True
    if not shutil.which("crontab"):
        return False
    return "# hexiel-jobs" in subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout


def describe(job: dict) -> str:
    last = (datetime.datetime.fromtimestamp(job["last_run"]).strftime("%Y-%m-%d %H:%M")
            if job.get("last_run") else "never")
    return (f"{job['id']}  {job['name']}  [{job['schedule']}]  {job.get('access', 'readonly')}  "
            f"last run: {last} ({job.get('last_status') or '—'})  in {job['cwd']}")
