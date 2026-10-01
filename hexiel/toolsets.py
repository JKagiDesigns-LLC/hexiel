"""Optional tool groups, loaded only when useful — every tool definition is sent
with every request, so groups a session doesn't need stay out of the prompt.

A group loads automatically when the project looks like it needs it (e.g. data
files present), when listed in settings.toolsets, or on demand: the always-
present, tiny `load_tools` tool lets the model pull a group in mid-session."""

from pathlib import Path

GROUPS = {
    "data": "spreadsheets & data: CSV/Excel/Parquet analysis with SQL, local SQLite databases, "
            "Excel read/write, charts",
}
DATA_EXTS = {".csv", ".tsv", ".xlsx", ".xlsm", ".xls", ".parquet", ".db", ".sqlite", ".sqlite3"}
SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".hexiel"}


def _group_tools(group: str) -> list:
    if group == "data":
        from .datatools import data_tools
        return data_tools()
    return []


def _looks_like(group: str, cwd: str) -> bool:
    if group != "data":
        return False
    root = Path(cwd)
    try:
        for child in root.iterdir():
            if child.name in SKIP:
                continue
            if child.is_file() and child.suffix.lower() in DATA_EXTS:
                return True
            if child.is_dir():
                if any(p.suffix.lower() in DATA_EXTS for p in list(child.iterdir())[:200] if p.is_file()):
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
