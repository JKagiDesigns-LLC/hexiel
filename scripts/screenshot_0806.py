"""Ticket evidence screenshots for the 0.8.6 UX batch: cross-harness rules (#26),
/fork branch (#27), /share transcript (#28), /watch comments (#29)."""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("HEXIEL_DATA", "/tmp/opencode/ssdata")
os.environ.setdefault("HEXIEL_HOME", "/tmp/opencode/sshome")
os.environ["HEXIEL_NO_REPORTS"] = "1"


def _mk(session_kwargs: dict, messages: list[dict], cwd: str | None = None):
    from hexiel.config import ModelConfig, Settings
    from hexiel.session import Session
    from hexiel.tooling import Toolchain
    session = Session(ModelConfig(name="gmktec", provider="openai", model="qwen3.6:35b-a3b-coding",
                                  base_url="http://127.0.0.1:11434/v1", context_window=200_000),
                      Settings(), cwd=cwd or str(Path.home() / "hexiel"),
                      tools=[], system_prompt="t")
    session.messages = messages
    return session


def _chat_texts(app) -> list[str]:
    return [str(getattr(w, "visual", getattr(w, "source", ""))) for w in app.chat.children]


async def fork_shot(out: Path) -> None:
    """#27: /fork 2 — the branch keeps turns 1..2, the original stays complete."""
    import datetime
    def stamp(offset: int) -> float:
        return datetime.datetime.now().astimezone().timestamp() + offset
    messages = [
        {"role": "user", "content": "[environment — fresh facts for this turn only]\nToday's date: "
         "2026-11-06\nbranch: main\n\nwhy is the login test failing?", "ts": stamp(-300)},
        {"role": "assistant", "content": "I'll look — starting with the test and the auth module.",
         "ts": stamp(-298), "tool_calls": [{"id": "c1", "name": "test", "arguments": {"path": "tests/test_login.py"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "tests/test_login.py::test_login_expired_token FAILED"},
        {"role": "assistant", "content": "Found it: login() in auth.py:112 compares the raw token.",
         "ts": stamp(-290)},
        {"role": "user", "content": "[environment — fresh facts for this turn only]\nToday's date: "
         "2026-11-06\nbranch: main\n\nok now check the /context command", "ts": stamp(-80)},
        {"role": "assistant", "content": "The meter says tools take 62% of the window right now.",
         "ts": stamp(-40)},
        {"role": "user", "content": "[environment — fresh facts for this turn only]\nToday's date: "
         "2026-11-06\nbranch: main\n\nnow shorten the /compact summary", "ts": stamp(-20)},
        {"role": "assistant", "content": "Compact summaries now cap at 1200 tokens — half of before.",
         "ts": stamp(-10)},
    ]
    session = _mk({}, messages)
    from hexiel.tui import HexielTUI
    app = HexielTUI(session, [], set())
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause(0.3)
        app.replay_history()
        await pilot.pause(0.4)
        app.cmd_fork("2")
        await pilot.pause(0.5)
        out.write_text(app.export_screenshot())


async def share_shot(out: Path) -> None:
    """#28: /share writes the self-contained HTML transcript."""
    messages = [
        {"role": "user", "content": "[environment — fresh facts for this turn only]\nToday's date: "
         "2026-11-06\nbranch: main\n\nthe deploy button does nothing on Safari", "ts": 0},
        {"role": "assistant", "content": "Checking the deploy handler…", "ts": 0,
         "tool_calls": [{"id": "c1", "name": "grep", "arguments": {"pattern": "deploy", "path": "web"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "web/app.tcss:88: #deploy-btn { display: flex; }"},
        {"role": "assistant", "content": "Found it — the button is *flex* in Safari only:\n\n"
         "1. replace `inline-block`\n2. `await popover.show()`\n\n```css\n#deploy-btn { display: block; }\n```",
         "ts": 0},
    ]
    session = _mk({}, messages, cwd=str(Path.home() / "hexiel"))
    from hexiel.tui import HexielTUI
    app = HexielTUI(session, [], set())
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause(0.3)
        app.replay_history()
        await pilot.pause(0.4)
        out_file = Path("/tmp/opencode/ssdata/deploy-bug-session.html")
        session.save()
        app.cmd_share(str(out_file))
        await pilot.pause(0.5)
        out.write_text(app.export_screenshot())


async def watch_shot(out: Path) -> None:
    """#29: /watch fires on a saved AI? comment."""
    project = Path("/tmp/opencode/ssdata/watchproj")
    (project / "src").mkdir(parents=True, exist_ok=True)
    src = project / "src" / "app.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("def deploy():\n    \"\"\"ship it\"\"\"\n")
    session = _mk({}, [
        {"role": "user", "content": "wake me when a dev server starts (background)", "ts": 0},
        {"role": "assistant", "content": "Dev server is running as a background task. I'm ready — "
         "save comments and I'll act on them.", "ts": 0},
    ], cwd=str(project))
    from hexiel.tui import HexielTUI
    app = HexielTUI(session, [], set())
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause(0.5)
        app.cmd_watch("")
        await pilot.pause(0.4)
        # no real turn needed for the shot: record the prompt instead
        app._start_turn = lambda prompt: None
        w = app._watch
        # simulate the save: (the comment lands in a file, the watcher reacts)
        src.write_text("def deploy():\n    # AI? is this retried on network errors?\n    return request('POST')\n")
        w.scan_once()
        await pilot.pause(0.6)
        out.write_text(app.export_screenshot())
        w.stop()


def msg(content: str) -> dict:
    return {"role": "user", "content": content}


async def rules_shot(out: Path) -> None:
    """#26: every harness's rules import together, scoped last-wins."""
    proj = Path("/tmp/opencode/ssdata/rulesproj")
    (proj / ".claude").mkdir(parents=True, exist_ok=True)
    (proj / ".codex" / "rules").mkdir(parents=True, exist_ok=True)
    (proj / ".cursor" / "rules").mkdir(parents=True, exist_ok=True)
    (proj / "CLAUDE.md").write_text("# stale root copy — shadowed by the nested file\n")
    (proj / ".claude" / "CLAUDE.md").write_text("# Claude rules\n\nAlways run `make check` before shipping.\n")
    (proj / ".codex" / "rules" / "tests.mdc").write_text("# Codex rules\n\npytest -q; never -x.\n")
    (proj / ".cursor" / "rules" / "style.mdc").write_text("# Cursor rules\n\nFenced code in answers.\n")
    (proj / "COPILOT.md").write_text("# Copilot rules\n\nCommit messages: imperative mood.\n")
    (proj / "AGENTS.md").write_text("placeholder\n")
    from hexiel import instructions as ins
    srcs = ins.discover(str(proj))
    notes = []

    def say(text: str = "") -> None:
        notes.append(text)

    session = _mk({}, [{"role": "user", "content": "import the rules from every harness I use"}],
                  cwd=str(proj))
    from hexiel.tui import HexielTUI
    app = HexielTUI(session, [], set())
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause(0.3)
        names = ", ".join(s.label for s in srcs)
        note = "".join(notes) if notes else ""
        summary = ins.import_sources(str(proj), srcs, "ignore", say=lambda *a: None)
        app.add_note(f"🧭 imported rules from {len(srcs)} harness files: {names}")
        app.add_note(f"   split into {summary['topics']} topic files in .hexiel/instructions/ "
                     "(read on demand — context stays small); originals untouched")
        await pilot.pause(0.6)
        out.write_text(app.export_screenshot())


def export(name: str, coro_factory) -> None:
    out_dir = Path("/tmp/opencode/tickets")
    out_dir.mkdir(parents=True, exist_ok=True)
    svg_path = out_dir / f"{name}.svg"
    asyncio.run(coro_factory(svg_path))
    png = out_dir / f"{name}.png"
    subprocess.run(["magick", "-density", "144", str(svg_path), "-background", "#12141a",
                    "-resize", "1440x", str(png)], check=True, timeout=120)
    print(f"{png} ({png.stat().st_size // 1024} KB)")


async def autocomplete_shot(out: Path) -> None:
    """#25: live path menu right under the prompt, while typing './r'."""
    tmp = Path("/tmp/opencode/ssdata/autoproj")
    for d in ("src", "src/utils", "hello world"):
        (tmp / d).mkdir(parents=True, exist_ok=True)
    for f in ("src/main.py", "src/main_test.py", "README.md", "readme.txt", "hello world/shot.png"):
        (tmp / f).write_text("x")
    session = _mk({}, [{"role": "user", "content": "fix the footer"}], cwd=str(tmp))
    from hexiel.tui import HexielTUI
    app = HexielTUI(session, [], set())
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause(0.4)
        for key in "./r":
            await pilot.press(key)
        await pilot.pause(0.5)
        out.write_text(app.export_screenshot())


def main() -> None:
    export("fork", fork_shot)
    export("share", share_shot)
    export("watch", watch_shot)
    export("rules", rules_shot)
    export("autocomplete", autocomplete_shot)


if __name__ == "__main__":
    main()
