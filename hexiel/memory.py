"""File-based persistent memory: MEMORY.md index + one markdown file per fact.

Structure mirrors Claude Code's auto-memory: each memory is a file with
frontmatter (name, description, type) and the MEMORY.md index is one line
per memory. Both a global dir and a per-project dir are supported; the
agent writes them with the normal Write/Edit tools.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from .config import memory_dir


@dataclass
class Memory:
    path: Path
    name: str
    description: str
    type: str
    scope: str  # "global" | "project"

    @classmethod
    def from_file(cls, path: Path, scope: str) -> "Memory | None":
        try:
            text = path.read_text()
        except OSError:
            return None
        if path.name == "MEMORY.md":
            return None
        match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
        name = path.stem
        description = type_ = ""
        if match:
            for line in match.group(1).splitlines():
                if line.startswith("name:"):
                    name = line[5:].strip() or name
                elif line.startswith("description:"):
                    description = line[12:].strip()
                elif line.lower().startswith("type:"):
                    type_ = line.split(":", 1)[1].strip()
        return cls(path=path, name=name, description=description, type=type_, scope=scope)


def project_memory_dir(cwd: str) -> Path:
    return Path(cwd) / ".hexiel" / "memory"


def load_memories(cwd: str) -> list[Memory]:
    memories: list[Memory] = []
    seen: set[Path] = set()
    for scope, directory in (("global", memory_dir()), ("project", project_memory_dir(cwd))):
        try:
            if not directory.is_dir() or directory in seen:
                continue
        except OSError:
            continue
        seen.add(directory)
        for path in sorted(directory.glob("*.md")):
            memory = Memory.from_file(path, scope)
            if memory:
                memories.append(memory)
    return memories


def index_text(cwd: str) -> str:
    """The MEMORY.md index content, for display via /memory."""
    lines: list[str] = []
    for scope, directory in (("global", memory_dir()), ("project", project_memory_dir(cwd))):
        index = directory / "MEMORY.md"
        if index.exists():
            try:
                lines.append(f"### {scope} ({index})\n")
                lines.append(index.read_text())
            except OSError:
                pass
    return "\n".join(lines) or "(no memories yet)"


def ensure_dirs(cwd: str) -> None:
    for directory in (memory_dir(), project_memory_dir(cwd)):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


AUTO_FACTS = "auto-facts.md"
AUTO_FACTS_CAP = 60


def save_auto_facts(cwd: str, facts: list[str]) -> int:
    """Merge durable facts (from compaction) into the project memory file
    auto-facts.md, skipping duplicates; returns how many were new."""
    facts = [" ".join(f.split()).strip(" -*") for f in facts]
    facts = [f for f in facts if 8 <= len(f) <= 300]
    if not facts:
        return 0
    directory = project_memory_dir(cwd)
    path = directory / AUTO_FACTS
    existing: list[str] = []
    try:
        if path.exists():
            existing = [ln[2:].strip() for ln in path.read_text().splitlines() if ln.startswith("- ")]
    except OSError:
        return 0
    seen = {e.lower() for e in existing}
    new = [f for f in facts if f.lower() not in seen and not seen.add(f.lower())]
    if not new:
        return 0
    merged = (existing + new)[-AUTO_FACTS_CAP:]
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nname: auto-facts\n"
                        "description: durable project facts captured automatically when context is compacted\n"
                        "type: project\n---\n\n" + "\n".join(f"- {f}" for f in merged) + "\n")
        index = directory / "MEMORY.md"
        line = f"- [Auto-captured facts]({AUTO_FACTS}) — build/test commands, conventions, preferences"
        current = index.read_text() if index.exists() else ""
        if AUTO_FACTS not in current:
            index.write_text(current + ("" if current.endswith("\n") or not current else "\n") + line + "\n")
    except OSError:
        return 0
    return len(new)


def parse_durable_facts(summary: str) -> list[str]:
    """Bullets under a '## Durable Facts' heading of a compaction summary."""
    m = re.search(r"^##\s*Durable Facts\s*\n(.*?)(?=^##\s|\Z)", summary, re.S | re.M)
    if not m:
        return []
    return [ln.strip()[2:] for ln in m.group(1).splitlines() if ln.strip().startswith(("- ", "* "))]

