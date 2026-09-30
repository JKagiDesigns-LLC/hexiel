"""OpenWebUI compatibility: run existing OpenWebUI plugins inside hexiel.

OpenWebUI defines three plugin conventions (https://docs.openwebui.com):
  - class Tools  — its public sync/async methods become callable tools; the
    user's own tools are exposed as <tool_name>_user_input kwargs; class-level
    ``Valves`` hold configuration.
  - class Pipe   — a single ``pipe(self, body: dict, ...)`` callable that
    receives the whole chat payload; exposed as one hexiel tool.
  - class Filter — inlet/outlet hooks (inlet is applied to the user message).

hexiel loads .py files from ``plugins/`` directories relative to the project or
the data dir (~/.local/share/hexiel/plugins), imports them, and maps each into
ToolDef entries with type-hint/docstring-derived JSON schemas (same convention
OpenWebUI uses). No porting effort: drop a community plugin file in plugins/ and
it appears in the registry.
"""

import asyncio
import importlib.util
import inspect
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import home_data_dir
from .tools import ToolDef, ToolResult


# ---------------------------------------------------------------- schema

def _json_type(annotation) -> str:
    ann = annotation if annotation is not inspect.Parameter.empty else str
    if ann in (int,):
        return "integer"
    if ann in (float,):
        return "number"
    if ann in (bool,):
        return "boolean"
    if ann in (list, dict):
        return "array" if ann is list else "object"
    return "string"


def _schema_for(func: callable) -> dict:
    """Build an OpenAI-style parameters schema from a method's type hints/docstring."""
    sig = inspect.signature(func)
    props: dict = {}
    required: list[str] = []
    doc = inspect.getdoc(func) or ""
    # OpenWebUI passes <param>_user_input automatically (the user's message) —
    # keep the slot but mark it; hexiel fills it at call time.
    for name, param in list(sig.parameters.items())[1:]:   # skip self
        if name.endswith("_user_input"):
            props[name] = {"type": "string", "description": "user-provided input",
                           "x_user_input": True}
        elif param.kind in (param.POSITIONAL_ONLY, param.KEYWORD_ONLY,
                            param.POSITIONAL_OR_KEYWORD):
            props[name] = {"type": _json_type(param.annotation)}
        elif param.kind is param.VAR_KEYWORD:
            props[name] = {"type": "object", "description": "extra fields"}
    if props:
        required = [n for n, p in props.items()
                    if inspect.signature(func).parameters[n].default is inspect.Parameter.empty
                    and not p.get("x_user_input")]
    # first docstring line(s) up to a blank line = description
    description = doc.split("\n\n")[0].strip() if doc else func.__name__
    return {"type": "object", "properties": props, "description": description,
            **({"required": required} if required else {})}


@dataclass
class LoadedPlugin:
    file: str
    name: str
    kind: str            # "tools" | "pipe" | "filter"
    instance: object     # the Tools/pipe/filter instance (valves already set)


def _plugins_dirs(cwd: str) -> list[Path]:
    dirs = [Path(home_data_dir()) / "plugins", Path.cwd() / "plugins"]
    if cwd:
        project = Path(cwd) / "plugins"
        if project not in dirs:
            dirs.append(project)
    return dirs


def discover_plugins(cwd: str) -> list[LoadedPlugin]:
    """Import plugin files and return Tools/Pipe/Filter instances found within."""
    plugins: list[LoadedPlugin] = []
    seen: set[Path] = set()
    for d in _plugins_dirs(cwd):
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.py")):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            loaded = _load_file(cwd, path)
            if loaded:
                plugins.extend(loaded)
    return plugins


def _load_file(cwd: str, path: Path) -> list[LoadedPlugin]:
    module_name = f"hexiel_plugin_{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - a bad plugin must not kill boot
        print(f"hexiel: plugin {path.name} failed to import: {exc!r}")
        return []
    found: list[LoadedPlugin] = []
    for attr in vars(module).values():
        if not inspect.isclass(attr):
            continue
        name = attr.__name__
        valves = getattr(attr, "Valves", None) or getattr(attr, "valves", None)
        if name == "Tools":
            instance = _instantiate(attr, valves)
            found.append(LoadedPlugin(str(path), name, "tools", instance))
        elif name == "Pipe":
            instance = _instantiate(attr, valves)
            found.append(LoadedPlugin(str(path), getattr(attr, "id", "pipe"), "pipe", instance))
        elif name == "Filter":
            instance = _instantiate(attr, valves)
            found.append(LoadedPlugin(str(path), name, "filter", instance))
    return found


def _instantiate(cls, valves):
    """Build the plugin, applying Valve defaults (env may override via name)."""
    try:
        instance = _call_with_defaults(cls)
    except Exception:  # noqa: BLE001
        instance = cls.__new__(cls)
    if valves is not None:
        try:
            setattr(instance, "valves", _call_with_defaults(valves))
        except Exception:  # noqa: BLE001
            pass
    return instance


def _call_with_defaults(cls):
    """Instantiate without args; plugin classes with default kwargs still work."""
    try:
        return cls()
    except TypeError:
        return cls.__new__(cls)   # no default construction: leave fields unset


# ---------------------------------------------------------------- mapping

def plugin_tool_defs(plugins: list[LoadedPlugin], cwd: str) -> list[ToolDef]:
    """Map plugin instances onto hexiel ToolDef entries (read-only is false:
    plugin tools can touch anything, so they pass the normal approval gate)."""
    defs: list[ToolDef] = []
    for plugin in plugins:
        if plugin.kind == "tools":
            tool_defs = _tools_instance_defs(plugin, cwd)
            defs.extend(tool_defs)
        elif plugin.kind == "pipe":
            defs.append(_pipe_tool_def(plugin, cwd))
    # Filter plugins: inlet applied by hexiel at send-time via apply_inlet()
    return defs


def _safe_name(prefix: str, method_name: str) -> str:
    name = re.sub(r"[^a-z0-9_]", "_", method_name.lower())
    prefix = re.sub(r"[^a-z0-9_]", "_", prefix.lower())
    return f"{prefix}_{name}".strip("_")[:64]


def _tools_instance_defs(plugin: LoadedPlugin, cwd: str) -> list[ToolDef]:
    out: list[ToolDef] = []
    for attr_name, method in inspect.getmembers(type(plugin.instance), inspect.isfunction):
        if attr_name.startswith("_"):
            continue
        # OpenWebUI conventions: methods starting with these are lifecycle
        if attr_name in ("valves", "startup", "shutdown"):
            continue
        schema = _schema_for(method)
        required = [n for n, pdef in schema["properties"].items()
                    if not pdef.get("x_user_input") and n in schema.get("required", [])]
        parameters = {"type": "object", "properties": schema["properties"],
                      "required": required}
        out.append(ToolDef(
            name=_safe_name(plugin.name, attr_name),
            description=schema.get("description", attr_name),
            parameters=parameters,
            readonly=False,      # unknown risk: always gate via approval
            always_ok=False,
            run=_plugin_runner(plugin.instance, method)))
    return out


def _pipe_tool_def(plugin: LoadedPlugin, cwd: str) -> ToolDef:
    return ToolDef(
        name=_safe_name("pipe", plugin.name),
        description=(plugin.instance.pipe.__doc__ or "OpenWebUI Pipe plugin").strip()
        if getattr(plugin.instance, "pipe", None) else "OpenWebUI Pipe plugin",
        parameters={"type": "object", "properties": {
            "messages": {"type": "array", "description": "messages payload",
                         "items": {"type": "object"}}},
            "required": ["messages"]},
        readonly=True,       # a Pipe only forwards to the model — safe to run
        always_ok=False,
        run=_pipe_runner(plugin.instance))


def _plugin_runner(instance, method):
    def run(args: dict, ctx) -> ToolResult:
        kwargs = {}
        for key, value in args.items():
            kwargs[key] = value
        try:
            result = method(instance, **kwargs)
        except TypeError:
            result = method(instance, *_flat_positional(args))
        if inspect.iscoroutine(result):
            result = _run_coroutine(result)
        return ToolResult(text=_tool_text(result))
    return run


def _pipe_runner(instance):
    def run(args: dict, ctx) -> ToolResult:
        body = {"messages": args.get("messages", []),
                "user": {"id": "hexiel", "name": "hexiel local"},
                "model": "hexiel"}
        pipe = getattr(instance, "pipe")
        result = pipe(body, _string_input(body.get("messages") and
                                          body["messages"][-1].get("content", "")))
        if inspect.iscoroutine(result):
            result = _run_coroutine(result)
        return ToolResult(text=_tool_text(result))
    return run


def _string_input(message_text: str):
    return message_text   # OpenWebUI __user__ stand-in, simplified


def _flat_positional(args: dict) -> list:
    return list(args.values())


def _run_coroutine(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                return pool.submit(asyncio.run, coro).result(timeout=120)
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


def _tool_text(result) -> str:
    if isinstance(result, (dict, list)):
        return json.dumps(result, indent=2, default=str)
    return str(result or "")


# Filter convention -----------------------------------------------------------

def apply_inlet(plugins: list[LoadedPlugin], user_text: str) -> tuple[str, list[str]]:
    """Run Filter.inlet over the user message; returns possibly-modified text +
    per-filter notes (errors, valve-driven transforms)."""
    notes: list[str] = []
    for plugin in plugins:
        if plugin.kind != "filter":
            continue
        inlet = getattr(plugin.instance, "inlet", None)
        if inlet is None or not callable(inlet):
            continue
        try:
            body = {"messages": [{"role": "user", "content": user_text}],
                    "user": {"id": "hexiel"}}
            out = inlet(body)   # getattr(instance, 'inlet') is already bound
            if inspect.iscoroutine(out):
                out = _run_coroutine(out)
            if isinstance(out, dict):
                msg = (out.get("messages") or [{}])[-1]
                if isinstance(msg, dict) and msg.get("content"):
                    user_text = str(msg["content"])
        except Exception as exc:  # noqa: BLE001
            notes.append(f"filter {plugin.name}: {exc!r}")
    return user_text, notes


def plugin_summary(plugins: list[LoadedPlugin]) -> str:
    if not plugins:
        return ""
    lines = ["OpenWebUI plugins loaded:"]
    for plugin in plugins:
        count = ""
        if plugin.kind == "tools":
            tools = [m for m in inspect.getmembers(type(plugin.instance), inspect.isfunction)
                     if not m[0].startswith("_")]
            count = f" ({len(tools)} tools)"
        lines.append(f"- {plugin.name} [{plugin.kind}]{count}")
    return "\n".join(lines)