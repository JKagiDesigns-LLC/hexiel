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
    except (OSError, ValueError):
        return []


def save(jobs: list[dict]) -> None:
    jobs_file().parent.mkdir(parents=True, exist_ok=True)
    jobs_file().write_text(json.dumps(jobs, indent=2))


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

def run_job(job: dict) -> tuple[bool, str]:
    """Run one job headless; returns (ok, answer)."""
    from .main import build_session
    session = build_session(job["cwd"], job.get("model") or None)
    full = job.get("access") == "full"
    session.on_approve = lambda tool, args: "yes" if full else "no"
    session.on_event = lambda *a, **k: None
    session.run_turn(f"[scheduled job: {job['name']}] {job['prompt']}\n\n(You are running unattended "
                     "on a schedule: do the task and end with a clear, self-contained report. "
                     + ("" if full else "You have read-only access: do not try to change files or "
                        "run commands that modify anything.") + ")")
    answer = next((m.get("content", "") for m in reversed(session.messages)
                   if m.get("role") == "assistant" and m.get("content")), "")
    return bool(answer), answer or "(the job produced no answer)"


def deliver(job: dict, ok: bool, answer: str) -> Path:
    inbox_dir().mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    path = inbox_dir() / f"{stamp}-{re.sub(r'[^a-z0-9]+', '-', job['name'].lower())[:40]}.md"
    path.write_text(f"# {job['name']}\n\n_{datetime.datetime.now():%Y-%m-%d %H:%M} · "
                    f"{'ok' if ok else 'no answer'} · {job['cwd']}_\n\n{answer}\n")
    _notify(f"hexiel job: {job['name']}", answer[:180])
    return path


def _notify(title: str, body: str) -> None:
    try:
        if shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-a", "hexiel", title, body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif shutil.which("osascript"):
            subprocess.Popen(["osascript", "-e", f'display notification "{body[:150]}" with title "{title}"'])
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
            save(jobs)
            try:
                ok, answer = runner(job)
            except Exception as exc:  # noqa: BLE001 - one failing job must not stop the others
                ok, answer = False, f"the job crashed: {exc!r}"
            job["last_status"] = "ok" if ok else "failed"
            save(jobs)
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
