"""Custom slash commands: markdown prompt files that become /name commands.

Looked up in (later wins): ~/.claude/commands, ~/.config/hexiel/commands,
<project>/.claude/commands, <project>/.hexiel/commands — the same format as
Claude Code, so existing commands work unchanged. A file in a subfolder
becomes /folder:name.

    ---
    description: Review the staged changes
    argument-hint: [focus area]
    ---
    Review `git diff --cached` with a focus on $ARGUMENTS. ($1 … $9 = words)
"""

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Command:
    name: str
    description: str
    argument_hint: str
    body: str
    path: Path

    def render(self, arguments: str) -> str:
        words = arguments.split()
        text = self.body.replace("$ARGUMENTS", arguments)
        text = re.sub(r"\$(\d)", lambda m: words[int(m.group(1)) - 1] if 0 < int(m.group(1)) <= len(words) else "", text)
        if "$ARGUMENTS" not in self.body and arguments and not re.search(r"\$\d", self.body):
            text += f"\n\n{arguments}"          # no placeholder: append what the user typed
        return text.strip()


def _parse(path: Path, name: str) -> Command | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    meta = {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip().strip('"\'')
        text = text[m.end():]
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return Command(name=name, description=meta.get("description") or first[:80],
                   argument_hint=meta.get("argument-hint", ""), body=text.strip(), path=path)


def discover(cwd: str) -> dict[str, Command]:
    from .config import home_config_dir
    found: dict[str, Command] = {}
    for root in (Path.home() / ".claude" / "commands", home_config_dir() / "commands",
                 Path(cwd) / ".claude" / "commands", Path(cwd) / ".hexiel" / "commands"):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            rel = path.relative_to(root).with_suffix("")
            name = ":".join(rel.parts).lower()
            cmd = _parse(path, name)
            if cmd:
                found[name] = cmd
    return found


# slash commands: (name, argument hint, what it does) — drives /help and the menu
BUILTIN = [
    ("help", "", "list commands and skills"),
    ("config", "", "view and change settings (session or permanent)"),
    ("model", "[name]", "pick from every model available right now (Ctrl+P), or switch by name"),
    ("models", "", "list models available on the Ollama server"),
    ("clear", "", "start a fresh conversation"),
    ("compact", "", "summarize history now to free up context"),
    ("auto", "[off|commits|all]", "auto-approve: cycle off / everything except git+svn commits / everything"),
    ("plan", "", "toggle plan mode: investigate and plan, change nothing"),
    ("delegate", "<request>|resume", "a strong model plans it, your worker model does each step (see /config for the models)"),
    ("solo", "<request>", "run this request normally, skipping the automatic big-task check"),
    ("undo", "", "revert the file changes hexiel made in its last turn"),
    ("jobs", "", "scheduled jobs (ask hexiel to schedule something to create one)"),
    ("inbox", "", "results from scheduled jobs"),
    ("usage", "", "tokens used this session (in / cached / out)"),
    ("permissions", "", "show allow / ask / deny rules (incl. this session's 'always')"),
    ("memory", "", "show what hexiel remembers about you and this project"),
    ("skills", "", "list installed skills"),
    ("mcp", "[add name]", "MCP servers: status, or add firebase/supabase/notion/github/playwright/filesystem"),
    ("ship", "[title]", "propose your changes as a GitLab merge request"),
    ("heal", "[what broke]", "fix a hexiel bug itself, review the diff, share it or keep it"),
    ("setup-gitlab", "<token>", "save a GitLab access token"),
    ("update", "", "install the newest hexiel release and restart"),
    ("exit", "", "quit hexiel"),
]
