"""Skills: SKILL.md files with (name, description) frontmatter, loaded on demand.

Search order: project dirs win over home, and .hexiel/skills wins over
Claude-Code's ~/.claude/skills, which is read too so existing skills keep
working without being copied.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from .config import home_data_dir


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    body: str


_FOLDERS_SKIPPED = {".git", "node_modules", "__pycache__", "vendor", ".venv", ".pixi"}


def skills_root_dirs(cwd: str) -> list[Path]:
    """Lowest priority first (later entries override earlier ones)."""
    return [
        Path.home() / ".claude" / "skills",   # existing Claude skills, for compat
        home_data_dir() / "skills",           # ~/.local/share/hexiel/skills
        Path(cwd) / "skills",                 # repo-bundled skills when working in hexiel itself
        Path(cwd) / ".claude" / "skills",
        Path(cwd) / ".hexiel" / "skills",
    ]


def discover(cwd: str) -> list[Skill]:
    skills: dict[str, Skill] = {}
    for directory in skills_root_dirs(cwd):
        for path in _iter_skill_files(directory):
            skill = _parse(path)
            if skill and skill.name:
                skills[skill.name] = skill
    return sorted(skills.values(), key=lambda s: s.name)


def _iter_skill_files(root: Path):
    if not root.is_dir():
        return
    try:
        for path in sorted(root.rglob("SKILL.md")):
            if any(part in _FOLDERS_SKIPPED for part in path.parts):
                continue
            yield path
    except OSError:
        return


def _parse(path: Path) -> Skill | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    match = re.match(r"^\s*---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not match:
        return None
    header: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            header[key.strip().lower()] = value.strip()
    name = header.get("name") or path.parent.name
    return Skill(name=name, description=header.get("description", ""),
                 path=path, body=text[match.end():].strip())


def find(skills: list[Skill], name: str) -> Skill | None:
    for skill in skills:
        if skill.name == name:
            return skill
    return None


def command_names(skills: list[Skill]) -> set[str]:
    return {skill.name for skill in skills}


def prompt_section(skills: list[Skill]) -> str:
    if not skills:
        return "No skills are installed."
    lines = [f"- **{s.name}**: {s.description} ({s.path})" for s in skills]
    commands = ", ".join(f"/{s.name}" for s in skills)
    return ("Available skills:\n" + "\n".join(lines) +
            f"\nInvoke a skill with the `skill` tool, or its slash command: {commands}.")