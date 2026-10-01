"""Permission rules: allow / ask / deny tool calls by pattern (Claude Code syntax).

    Bash(git status:*)          command starting with "git status"
    Bash(npm run *)             glob over the whole command
    Read(./.env)  Edit(src/**)  file paths (relative to the project), glob patterns
    WebFetch(domain:github.com) URLs on that domain or its subdomains
    grep  mcp__github__*        a whole tool (globs allowed)

deny > ask > allow. Rules come from config.toml [permissions], the project's
.hexiel/settings.json, and Claude Code's .claude/settings.json /
settings.local.json (so existing rules just work). "Always" answers in the UI
add session rules scoped to that kind of call — never a blanket auto-approve.
"""

import fnmatch
import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

# hexiel tool -> Claude-Code-style rule name (so .claude/settings.json rules apply)
RULE_NAME = {"bash": "Bash", "powershell": "PowerShell", "python": "Python", "read": "Read",
             "write": "Write", "edit": "Edit", "grep": "Grep", "glob": "Glob", "list": "LS",
             "web_fetch": "WebFetch", "web_search": "WebSearch", "test": "Test",
             "container": "Container", "view_image": "Read"}
PATH_TOOLS = {"Read", "Write", "Edit", "Grep", "Glob", "LS"}
COMMAND_TOOLS = {"Bash", "PowerShell"}
_RULE = re.compile(r"^\s*([A-Za-z0-9_*.\-]+)\s*(?:\((.*)\))?\s*$")


@dataclass
class Rules:
    allow: list[str] = field(default_factory=list)
    ask: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)

    def extend(self, other: dict) -> None:
        for kind in ("allow", "ask", "deny"):
            getattr(self, kind).extend(str(r) for r in (other.get(kind) or []))


def load(cwd: str, config_permissions: dict | None = None) -> Rules:
    rules = Rules()
    rules.extend(config_permissions or {})
    for path in (Path(cwd) / ".hexiel" / "settings.json", Path(cwd) / ".claude" / "settings.json",
                 Path(cwd) / ".claude" / "settings.local.json"):
        try:
            rules.extend(json.loads(path.read_text()).get("permissions") or {})
        except (OSError, ValueError, AttributeError):
            continue
    return rules


def _argument(tool: str, args: dict) -> str:
    if tool in ("bash", "powershell"):
        return str(args.get("command", ""))
    if tool == "python":
        return str(args.get("code", ""))
    if tool == "web_fetch":
        return str(args.get("url", ""))
    return str(args.get("path") or args.get("file_path") or args.get("pattern") or "")


def _path_matches(pattern: str, value: str, cwd: str) -> bool:
    if not value:
        return False
    target = Path(value)
    target = (target if target.is_absolute() else Path(cwd) / target).resolve()
    pat = pattern[2:] if pattern.startswith("./") else pattern
    if pat.startswith("~/"):
        pat = str(Path.home() / pat[2:])
    if not Path(pat).is_absolute():
        try:
            rel = str(target.relative_to(Path(cwd).resolve()))
        except ValueError:
            rel = str(target)
        # "**" spans directories; a bare name also matches that file anywhere
        return (fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, pat.replace("**/", ""))
                or ("/" not in pat and fnmatch.fnmatch(target.name, pat)))
    return fnmatch.fnmatch(str(target), pat)


def _command_matches(spec: str, command: str) -> bool:
    command = " ".join(command.split())
    if spec.endswith(":*"):
        prefix = " ".join(spec[:-2].split())
        return command == prefix or command.startswith(prefix + " ")
    return fnmatch.fnmatch(command, " ".join(spec.split()))


def matches(rule: str, tool: str, args: dict, cwd: str) -> bool:
    m = _RULE.match(rule)
    if not m:
        return False
    name, spec = m.group(1), m.group(2)
    rule_name = RULE_NAME.get(tool, tool)
    if not (fnmatch.fnmatch(tool, name) or fnmatch.fnmatch(rule_name, name)
            or (name == "Edit" and rule_name == "Write")):      # Edit rules cover writes too
        return False
    if spec is None or spec.strip() in ("", "*"):
        return True
    value = _argument(tool, args)
    if rule_name in COMMAND_TOOLS:
        return _command_matches(spec, value)
    if rule_name == "WebFetch" and spec.startswith("domain:"):
        host = (urlparse(value).hostname or "").lower()
        domain = spec[len("domain:"):].strip().lower()
        return host == domain or host.endswith("." + domain)
    if rule_name in PATH_TOOLS:
        return _path_matches(spec.strip(), value, cwd)
    return fnmatch.fnmatch(value, spec)


def decide(rules: Rules, tool: str, args: dict, cwd: str) -> str | None:
    """'deny' | 'ask' | 'allow' | None (no rule matched → default behaviour)."""
    for kind in ("deny", "ask", "allow"):
        if any(matches(r, tool, args, cwd) for r in getattr(rules, kind)):
            return kind
    return None


def always_rule(tool: str, args: dict) -> str:
    """The narrow rule an 'always' answer adds: same tool, same kind of call."""
    name = RULE_NAME.get(tool, tool)
    if tool in ("bash", "powershell"):
        try:
            words = shlex.split(_argument(tool, args))
        except ValueError:
            words = _argument(tool, args).split()
        # "git status -s" -> git status:*, "ls -la" -> ls:*
        keep = words[:2] if len(words) > 1 and not words[1].startswith("-") else words[:1]
        return f"{name}({' '.join(keep)}:*)" if keep else name
    if tool == "web_fetch":
        host = urlparse(_argument(tool, args)).hostname
        return f"WebFetch(domain:{host})" if host else name
    return name
