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