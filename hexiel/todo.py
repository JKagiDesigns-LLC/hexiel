"""todo.md task tracking: hexiel's structured to-do list for multi-step work.

State lives in `<project>/.hexiel/todo.md` as GitHub-task-style markdown:

    # todo — <task title>
    <!-- hexiel: updated 2026-09-30 14:00 -->

    <!-- where we left off: <one line: the current checkpoint> -->

    - [ ] queued step
    - [~] in-progress step (hexiel keeps exactly one)
    - [x] completed step

Everything is plain markdown the agent (or the user) can read and edit with the
normal file tools; the `todo` tool enforces the in-progress rule and timestamps.
"""

import re
import datetime
import os
from dataclasses import dataclass
from pathlib import Path

MARKER = "hexiel"


def todo_path(cwd: str) -> Path:
    return Path(cwd) / ".hexiel" / "todo.md"


@dataclass
class TodoItem:
    state: str      # " " | "~" | "x"
    text: str


def _parse(text: str) -> tuple[str, str, list[TodoItem]]:
    """Return (title, left_off, items) from todo.md content."""
    title, left_off, items = "", "", []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# todo") and not title:
            title = re.sub(r"^#+\s*todo\s*[—-]\s*", "", stripped)
            continue
        if "where we left off:" in stripped:
            inner = re.search(r"where we left off:\s*(.*?)\s*--?>", stripped)
            if inner and inner.group(1):
                left_off = inner.group(1).strip()
            continue
        match = re.match(r"^[-*]\s+\[([ x~])\]\s+(.*)$", stripped)
        if match:
            items.append(TodoItem(state=match.group(1), text=match.group(2).strip()))
    return title, left_off, items


def serialise(title: str, left_off: str, items: list[TodoItem]) -> str:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    out = [f"# todo — {title}", f"<!-- {MARKER}: updated {now} -->", ""]
    if left_off:
        out += [f"<!-- where we left off: {left_off} -->", ""]
    for item in items:
        out.append(f"- [{item.state}] {item.text}")
    return "\n".join(out) + "\n"


def read_todos(cwd: str) -> tuple[str, str, list[TodoItem]]:
    path = todo_path(cwd)
    try:
        if not path.exists():
            return "", "", []
        return _parse(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return "", "", []


def summary_line(cwd: str) -> str:
    """One-line status for the status bar / resume notes."""
    _, left_off, items = read_todos(cwd)
    if not items:
        return ""
    done = sum(1 for i in items if i.state == "x")
    progress = next((i.text for i in items if i.state == "~"), "")
    base = f"todo {done}/{len(items)} done"
    if progress:
        base += f" — in progress: {progress[:40]}"
    if left_off:
        base += f" | left off: {left_off[:40]}"
    return base


# ---------------------------------------------------------------- tool actions

def action_create(args: dict, cwd: str) -> str:
    title = args["title"]
    steps = [str(s).strip() for s in (args.get("steps") or []) if str(s).strip()]
    if not steps:
        return "error: provide at least one step"
    existing_title, _, existing = read_todos(cwd)
    if existing and existing_title == title:
        return (f"todo.md already covers '{title}' with {len(existing)} items; "
                "add or restate steps instead of overwriting progress")
    items = [TodoItem(state=" ", text=s) for s in steps]
    path = todo_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialise(title, "starting", items))
    return f"todo.md created ({len(items)} steps) at {path}"


def action_add(args: dict, cwd: str) -> str:
    title, left_off, items = read_todos(cwd)
    if not items and not title:
        return "error: no todo list yet — create one first"
    text = args["step"].strip()
    if not text:
        return "error: empty step"
    after = args.get("after")   # insert after this item text, else append
    item = TodoItem(state=" ", text=text)
    insert_at = len(items)
    for i, existing in enumerate(items):
        if after and after.lower() in existing.text.lower():
            insert_at = i + 1
            break
    items.insert(insert_at, item)
    _write(cwd, title, left_off, items)
    return f"step added at position {insert_at + 1}: {text}"


def action_update(args: dict, cwd: str) -> str:
    title, left_off, items = read_todos(cwd)
    if not items:
        return "error: no todo list yet"
    needle = args["step"].strip().lower()
    idx = next((i for i, it in enumerate(items) if needle in it.text.lower()), None)
    if idx is None:
        states = "\n".join(f"  [{i.state}] {i.text}" for i in items)
        return f"error: no step matches '{needle}'. Current items:\n{states}"
    state = args.get("state", "x")
    if state not in (" ", "~", "x"):
        return "error: state must be ' ' (queued), '~' (in-progress), or 'x' (done)"
    if state == "~":
        for other in items:
            if other.state == "~":
                other.state = " "
    items[idx].state = state
    _write(cwd, title, left_off, items)
    remaining = [i for i in items if i.state != "x"]
    done = f" step '{items[idx].text}' -> {state!r}"
    if state == "x":
        done += f" ({sum(1 for i in items if i.state == 'x')}/{len(items)} done)"
    return done + ("" if remaining else " — todo complete")


def action_left_off(args: dict, cwd: str) -> str:
    title, _, items = read_todos(cwd)
    if not title and not items:
        return "error: no todo list yet"
    note = args["note"].strip()
    _write(cwd, title, note[:200], items)
    return f"where-we-left-off updated: {note[:200]}"


def action_list(args: dict, cwd: str) -> str:
    title, left_off, items = read_todos(cwd)
    if not items:
        return "(no todo list; create one with the todo tool when a task is multi-step)"
    icon = {" ": " ", "~": "~", "x": "x"}
    lines = [f"todo — {title}" + (f" (left off: {left_off})" if left_off else "")]
    for item in items:
        lines.append(f"  [{icon[item.state]}] {item.text}")
    return "\n".join(lines)


def _write(cwd: str, title: str, left_off: str, items: list[TodoItem]) -> None:
    path = todo_path(cwd)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(serialise(title or "(untitled)", left_off, items))
    except OSError:
        pass


ACTIONS = {"create": action_create, "add": action_add, "update": action_update,
           "left_off": action_left_off, "list": action_list}


# ---------------------------------------------------------------- automatic upkeep
# Done in Python by the harness, so it costs no model tokens: the checkpoint
# stays accurate even when the model forgets, and a crash mid-task always
# leaves a true "where we left off".

AUTO_MIN_TOOLS = 3   # a request becomes a tracked task once it needs this many tool calls


def _request_title(request: str) -> str:
    first = " ".join(request.strip().split("\n")[0].split())
    return first if len(first) <= 80 else first[:79].rstrip() + "…"


def auto_track(cwd: str, request: str) -> str | None:
    """Make sure the current request is a tracked, in-progress task; returns
    the item text (None if nothing could be written)."""
    text = _request_title(request)
    if not text:
        return None
    title, left_off, items = read_todos(cwd)
    if not any(i.text == text for i in items):
        for other in items:
            if other.state == "~":
                other.state = " "
        items.append(TodoItem(state="~", text=text))
    _write(cwd, title or text, left_off or "starting", items)
    return text


def auto_checkpoint(cwd: str, note: str) -> None:
    """Refresh the where-we-left-off line (only when a todo list exists)."""
    title, _, items = read_todos(cwd)
    if items:
        _write(cwd, title, " ".join(note.split())[:200], items)


def auto_finish(cwd: str, item_text: str, ok: bool, note: str) -> None:
    title, _, items = read_todos(cwd)
    for item in items:
        if item.text == item_text:
            item.state = "x" if ok else "~"
    _write(cwd, title, " ".join(note.split())[:200], items)

