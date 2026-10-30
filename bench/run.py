#!/usr/bin/env python3
"""hexiel benchmark: accuracy vs speed vs cloud cost.

Each task is a small project (seed/) plus a prompt; hidden tests (hidden/) decide whether the result is right.
Each *configuration* is a way of running hexiel: local model alone, local + advice, delegated, delegated + review,
cloud model alone. For every (task, configuration) the runner builds a fresh copy of the seed in a temp folder, runs
`hexiel -p` with an isolated config, then runs the hidden tests and records pass/fail, wall time and tokens per model.

    python bench/run.py --self-check                      # validate the tasks (seed fails, reference solution passes)
    python bench/run.py --list
    python bench/run.py --tasks intervals --configs cloud
    python bench/run.py --configs local,delegate+review   # all tasks, those two configurations

It uses your real model profiles (names given with --local / --cloud) from ~/.config/hexiel/config.toml; nothing in
your own config, sessions or projects is touched. NOTE: local runs occupy the local model server for their whole duration.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from hexiel import settings_edit  # noqa: E402  (pure TOML line editing helpers)

# Every run starts from these, so only the configuration below decides which models work: the router, overflow,
# step fallback and per-tool helper models in a personal config would otherwise quietly bring in other models.
ISOLATE = {"router_profile": "", "overflow_profile": "", "worker_fallback_profile": "", "escalate_mode": "off"}

# LOCAL / CLOUD are replaced by the profile names given on the command line
CONFIGS = {
    "local": {"model": "LOCAL", "delegate": False,
              "settings": {"auto_delegate": "off", "consult_mode": "off", "planner_profile": "", "delegate_review": "off"}},
    "local+advice": {"model": "LOCAL", "delegate": False,
                     "settings": {"auto_delegate": "off", "consult_mode": "auto", "planner_profile": "CLOUD",
                                  "escalate_mode": "auto", "delegate_review": "off"}},
    "delegate": {"model": "LOCAL", "delegate": True,
                 "settings": {"auto_delegate": "off", "consult_mode": "off", "planner_profile": "CLOUD",
                              "executor_profile": "LOCAL", "delegate_review": "off", "delegate_approval": False}},
    "delegate+review": {"model": "LOCAL", "delegate": True,
                        "settings": {"auto_delegate": "off", "consult_mode": "auto", "planner_profile": "CLOUD",
                                     "executor_profile": "LOCAL", "escalate_mode": "auto", "delegate_review": "always", "delegate_approval": False}},
    "cloud": {"model": "CLOUD", "delegate": False,
              "settings": {"auto_delegate": "off", "consult_mode": "off", "planner_profile": "", "delegate_review": "off"}},
}


def load_tasks(selected: list[str] | None) -> dict[str, dict]:
    tasks = {}
    for path in sorted((HERE / "tasks").glob("*/task.json")):
        task = json.loads(path.read_text())
        task["id"], task["dir"] = path.parent.name, path.parent
        if not selected or task["id"] in selected:
            tasks[task["id"]] = task
    missing = set(selected or []) - set(tasks)
    if missing:
        raise SystemExit(f"unknown task(s): {', '.join(sorted(missing))} (have: {', '.join(p.parent.name for p in (HERE / 'tasks').glob('*/task.json'))})")
    return tasks


def make_workdir(task: dict, root: Path) -> Path:
    work = root / task["id"]
    shutil.copytree(task["dir"] / "seed", work)
    git = ["git", "-c", "user.email=bench@example.com", "-c", "user.name=bench"]
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    subprocess.run([*git, "add", "-A"], cwd=work, check=True)
    subprocess.run([*git, "commit", "-qm", "seed"], cwd=work, check=True, capture_output=True)
    return work


def verify(task: dict, work: Path) -> tuple[bool, str]:
    hidden = work / "hidden_tests"
    if hidden.exists():
        shutil.rmtree(hidden)
    shutil.copytree(task["dir"] / "hidden", hidden)
    try:
        proc = subprocess.run(["bash", "-lc", task["verify"]], cwd=work, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        return False, "verification timed out"
    out = (proc.stdout + proc.stderr).strip().splitlines()
    return proc.returncode == 0, "\n".join(out[-25:])


def self_check(tasks: dict[str, dict]) -> bool:
    ok = True
    for task in tasks.values():
        with tempfile.TemporaryDirectory(prefix="hexiel-bench-") as tmp:
            work = make_workdir(task, Path(tmp))
            seed_pass, _ = verify(task, work)
            solution = task["dir"] / "solution"
            for src in solution.rglob("*"):
                if src.is_file():
                    dst = work / src.relative_to(solution)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
            sol_pass, tail = verify(task, work)
        good = (not seed_pass) and sol_pass
        ok &= good
        print(f"{'ok  ' if good else 'FAIL'} {task['id']:<10} seed {'passes (BAD: nothing to do)' if seed_pass else 'fails'}, "
              f"reference solution {'passes' if sol_pass else 'FAILS'}")
        if not sol_pass:
            print("      " + tail.replace("\n", "\n      "))
    return ok


def build_config(base_text: str, spec: dict, local: str, cloud: str, home: Path) -> None:
    sub = {"LOCAL": local, "CLOUD": cloud}
    text = settings_edit.set_toml_value(base_text, None, "default", sub[spec["model"]])
    text = re.sub(r"(?m)^\[tool_models\]\n(?:(?!\[).*\n?)*", "", text)        # tool output stays raw: no helper models
    settings = dict(ISOLATE, **spec["settings"], notify=False, auto_approve=True)
    for key, value in settings.items():
        value = sub.get(value, value) if isinstance(value, str) else value
        text = settings_edit.set_toml_value(text, "settings", key, value)
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(text)


def hexiel_command() -> list[str]:
    venv = ROOT / ".venv" / "bin" / "hexiel"
    return [str(venv)] if venv.exists() else ["hexiel"]


def run_one(task: dict, config_name: str, base_text: str, local: str, cloud: str, keep: bool) -> dict:
    spec = CONFIGS[config_name]
    models = tomllib.loads(base_text)["models"]
    ids = {"local": models[local]["model"], "cloud": models[cloud]["model"]}
    record = {"task": task["id"], "config": config_name, "passed": False, "seconds": 0.0, "timed_out": False,
              "cloud_tokens": 0, "local_tokens": 0, "requests": 0, "error": ""}
    with tempfile.TemporaryDirectory(prefix="hexiel-bench-") as tmp:
        tmp = Path(tmp)
        work = make_workdir(task, tmp / "work")
        build_config(base_text, spec, local, cloud, tmp / "home")
        env = dict(os.environ, HEXIEL_HOME=str(tmp / "home"), HEXIEL_DATA=str(tmp / "data"), HEXIEL_NO_UPDATE_CHECK="1")
        prompt = ("/delegate " if spec["delegate"] else "") + task["prompt"]
        model = {"LOCAL": local, "CLOUD": cloud}[spec["model"]]
        started = time.time()
        try:
            proc = subprocess.run([*hexiel_command(), "-m", model, "-y", "-p", prompt], cwd=work, env=env,
                                  capture_output=True, text=True, timeout=task.get("timeout", 900))
            stderr, stdout = proc.stderr, proc.stdout
        except subprocess.TimeoutExpired as exc:
            record["timed_out"] = True
            stderr, stdout = (exc.stderr or b"").decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or ""), ""
        record["seconds"] = round(time.time() - started, 1)
        for line in stderr.splitlines():
            if line.startswith("HEXIEL_USAGE "):
                try:
                    usage = json.loads(line[len("HEXIEL_USAGE "):])
                except ValueError:
                    continue
                for model_id, u in usage.items():
                    tokens = u.get("prompt", 0) + u.get("completion", 0)
                    record["requests"] += u.get("requests", 0)
                    if model_id == ids["cloud"] and ids["cloud"] != ids["local"]:
                        record["cloud_tokens"] += tokens
                    else:
                        record["local_tokens"] += tokens
        if cloud == local:        # same model on both sides: everything counts as local
            record["local_tokens"] += record["cloud_tokens"]
            record["cloud_tokens"] = 0
        passed, tail = verify(task, work)
        record["passed"] = passed
        if not passed:
            record["error"] = tail[-1500:]
        record["changed_files"] = len([ln for ln in subprocess.run(["git", "status", "--porcelain"], cwd=work, capture_output=True,
                                                                  text=True).stdout.splitlines() if "hidden_tests" not in ln])
        if keep:
            shutil.copytree(work, HERE / "results" / "work" / f"{task['id']}-{config_name}", dirs_exist_ok=True)
        log_dir = HERE / "results" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{task['id']}-{config_name}.log").write_text(
            f"PROMPT: {prompt}\n\n--- stdout\n{stdout}\n--- stderr\n{stderr[-20000:]}\n--- verify\n{tail if not passed else 'PASSED'}\n")
    return record


def fmt_time(seconds: float) -> str:
    return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


def table(records: list[dict]) -> str:
    tasks = sorted({r["task"] for r in records})
    configs = [c for c in CONFIGS if any(r["config"] == c for r in records)]
    width = 26
    lines = ["| task | " + " | ".join(configs) + " |", "|---|" + "---|" * len(configs)]
    for t in tasks:
        cells = []
        for c in configs:
            rs = [r for r in records if r["task"] == t and r["config"] == c]
            if not rs:
                cells.append("—")
                continue
            ok = sum(r["passed"] for r in rs)
            avg_s = sum(r["seconds"] for r in rs) / len(rs)
            cloud = sum(r["cloud_tokens"] for r in rs) / len(rs)
            mark = "PASS" if ok == len(rs) else ("FAIL" if ok == 0 else f"{ok}/{len(rs)}")
            cells.append(f"{mark} {fmt_time(avg_s)} {cloud / 1000:.0f}k☁")
        lines.append(f"| {t} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("| configuration | passed | mean time | mean cloud tokens | mean local tokens |")
    lines.append("|---|---|---|---|---|")
    for c in configs:
        rs = [r for r in records if r["config"] == c]
        lines.append(f"| {c} | {sum(r['passed'] for r in rs)}/{len(rs)} | {fmt_time(sum(r['seconds'] for r in rs) / len(rs))} | "
                     f"{sum(r['cloud_tokens'] for r in rs) / len(rs) / 1000:.1f}k | {sum(r['local_tokens'] for r in rs) / len(rs) / 1000:.1f}k |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks", help="comma-separated task ids (default: all)")
    ap.add_argument("--configs", default="local,delegate+review,cloud", help=f"comma-separated: {', '.join(CONFIGS)}")
    ap.add_argument("--local", default="gmktec", help="profile name of the local model")
    ap.add_argument("--cloud", default="anthropic-sonnet", help="profile name of the cloud model")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--self-check", action="store_true", help="validate the tasks and exit")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--keep", action="store_true", help="keep each finished project under bench/results/work/")
    args = ap.parse_args()

    tasks = load_tasks(args.tasks.split(",") if args.tasks else None)
    if args.list:
        for t in tasks.values():
            print(f"{t['id']:<10} {t['title']}")
        return 0
    if args.self_check:
        return 0 if self_check(tasks) else 1

    configs = args.configs.split(",")
    bad = [c for c in configs if c not in CONFIGS]
    if bad:
        raise SystemExit(f"unknown configuration(s): {', '.join(bad)}")
    base_path = Path(os.environ.get("HEXIEL_HOME", Path.home() / ".config" / "hexiel")) / "config.toml"
    base_text = base_path.read_text()
    models = tomllib.loads(base_text).get("models", {})
    for name in (args.local, args.cloud):
        if name not in models:
            raise SystemExit(f"profile {name!r} is not in {base_path} (have: {', '.join(models)})")

    records = []
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    total = len(tasks) * len(configs) * args.repeat
    n = 0
    for task in tasks.values():
        for config in configs:
            for _ in range(args.repeat):
                n += 1
                print(f"[{n}/{total}] {task['id']} · {config} …", flush=True)
                rec = run_one(task, config, base_text, args.local, args.cloud, args.keep)
                records.append(rec)
                print(f"      {'PASS' if rec['passed'] else 'FAIL'}  {fmt_time(rec['seconds'])}  cloud {rec['cloud_tokens']:,} tok  "
                      f"local {rec['local_tokens']:,} tok" + ("  (timed out)" if rec["timed_out"] else ""), flush=True)
                (out_dir / f"{stamp}.json").write_text(json.dumps(records, indent=2))     # saved after every run
    print("\n" + table(records))
    print(f"\nraw results: {out_dir / (stamp + '.json')}   logs: {out_dir / 'logs'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
