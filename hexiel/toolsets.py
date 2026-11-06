"""Optional tool groups, loaded only when useful — every tool definition is sent
with every request, so groups a session doesn't need stay out of the prompt.

A group loads automatically when the project looks like it needs it (e.g. data
files present), when listed in settings.toolsets, or on demand: the always-
present, tiny `load_tools` tool lets the model pull a group in mid-session."""

from pathlib import Path

GROUPS = {
    "data": "spreadsheets & data: CSV/Excel/Parquet analysis with SQL, local SQLite databases, "
            "Excel read/write, charts, Jupyter notebook (.ipynb) cell editing",
    "schedule": "scheduled/recurring jobs (cron-like, runs even when hexiel is closed)",
    "docs": "question-answering over PDFs / Word / Markdown documents with page citations",
    "knowledge": "the user's synced reference documentation library (versioned docs for the "
                 "technologies they use): knowledge_search",
    "research": "deep web research: many searches + pages read in parallel, cited evidence pack",
    "desktop": "this machine's screen: screenshot capture, system clipboard read/write, "
               "OCR text extraction from images",
}
DATA_EXTS = {".csv", ".tsv", ".xlsx", ".xlsm", ".xls", ".parquet", ".db", ".sqlite", ".sqlite3",
             ".ipynb"}
SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".hexiel"}


def _group_tools(group: str) -> list:
    if group == "data":
        from .datatools import data_tools
        return data_tools()
    if group == "schedule":
        return [_schedule_tool()]
    if group == "docs":
        from .docqa import doc_tools
        return doc_tools()
    if group == "knowledge":
        from .knowledge import knowledge_tools
        return knowledge_tools()
    if group == "research":
        from .research import research_tools
        return research_tools()
    if group == "desktop":
        from .desktop import desktop_tools
        return desktop_tools()
    return []


def _schedule_tool():
    from . import jobs
    from .tools import ToolDef, text_result

    def run(args, ctx):
        action = args.get("action", "list")
        if action == "list":
            items = jobs.load()
            return text_result("\n".join(jobs.describe(j) for j in items) or "no scheduled jobs")
        if action == "remove":
            return text_result("removed" if jobs.remove(str(args.get("id", ""))) else "no such job", False)
        try:
            job = jobs.add(args.get("name", ""), args.get("schedule", ""), args.get("prompt", ""),
                           ctx.cwd, args.get("model", ""), args.get("access", "readonly"))
        except ValueError as exc:
            return text_result(f"error: {exc}", True)
        note = "" if jobs.installed() else f"\nscheduler: {jobs.install()}"
        return text_result(f"scheduled job {job['id']} '{job['name']}' [{job['schedule']}] in {ctx.cwd} "
                           f"({job['access']} access); results go to the hexiel inbox (/inbox){note}")
    return ToolDef(
        "schedule", "Create, list or remove recurring jobs that run hexiel headless on a schedule, "
        "even when hexiel is closed (results arrive in the inbox with a desktop notification). "
        "schedule is a 5-field cron expression in local time (e.g. '0 8 * * 1' = Mondays 08:00, "
        "'*/30 9-17 * * 1-5' = every 30 min in work hours). prompt must be self-contained. "
        "access: readonly (default) or full (may edit files / run commands).",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["add", "list", "remove"]},
            "name": {"type": "string"}, "schedule": {"type": "string"}, "prompt": {"type": "string"},
            "access": {"type": "string", "enum": ["readonly", "full"]}, "model": {"type": "string"},
            "id": {"type": "string"}}, "required": ["action"]},
        run=run)


DOC_EXTS = {".pdf", ".docx"}


def _looks_like(group: str, cwd: str) -> bool:
    if group == "knowledge":
        from .knowledge import has_content
        return has_content()
    if group == "desktop":
        from .desktop import _has_desktop
        return _has_desktop()
    if group == "docs":
        exts = DOC_EXTS
    elif group == "data":
        exts = DATA_EXTS
    else:
        return False
    root = Path(cwd)
    try:
        for child in root.iterdir():
            if child.name in SKIP:
                continue
            if child.is_file() and child.suffix.lower() in exts:
                return True
            if child.is_dir():
                if any(p.suffix.lower() in exts for p in list(child.iterdir())[:200] if p.is_file()):
                    return True
    except OSError:
        pass
    return False


def load(session, group: str) -> str:
    names = {t.name for t in session.tools}
    added = [t for t in _group_tools(group) if t.name not in names]
    session.tools.extend(added)
    session.loaded_toolsets.add(group)
    return ", ".join(t.name for t in added)


def install(session, settings) -> None:
    """Load the groups this session needs and add the tiny `load_tools` tool."""
    from .tools import ToolDef, text_result
    session.loaded_toolsets = set()
    wanted = getattr(settings, "toolsets", "auto")
    for group in GROUPS:
        if (wanted == "all" or (isinstance(wanted, list) and group in wanted)
                or (wanted == "auto" and _looks_like(group, session.cwd))):
            load(session, group)

    def run(args, ctx):
        group = str(args.get("group", "")).strip().lower()
        if group not in GROUPS:
            return text_result("unknown group — available: " + ", ".join(GROUPS), True)
        if group in session.loaded_toolsets:
            return text_result(f"'{group}' tools are already loaded")
        return text_result(f"loaded {group} tools: {load(session, group)} — use them from now on")
    session.tools.append(ToolDef(
        "load_tools", "Load an extra tool group when the task needs it: "
        + "; ".join(f"{k} ({v})" for k, v in GROUPS.items()),
        {"type": "object", "properties": {"group": {"type": "string", "enum": list(GROUPS)}}, "required": ["group"]},
        readonly=True, always_ok=True, run=run))
