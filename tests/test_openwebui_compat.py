"""Tests for the OpenWebUI compatibility layer: Tools/Pipe/Filter mapping."""

import json
import textwrap

from hexiel import openwebui_compat as owc

TOOLS_FILE = '''
class Tools:
    """community tools plugin (OpenWebUI convention)"""

    class Valves:
        api_url: str = "http://localhost:8080"

    def get_weather(self, city: str, units: str = "metric") -> dict:
        """Get current weather for a city."""
        return {"city": city, "units": units, "temp": 21}

    def _private_helper(self) -> str:
        return "never exposed"

    def search_notes(self, query: str, __user__: str = "") -> str:
        """Search notes for a query."""
        return f"notes for {query} (user {__user__})"
'''

PIPE_FILE = '''
class Pipe:
    id = "echo_pipe"
    name = "Echo"

    def pipe(self, body: dict, __user__: str = "") -> str:
        """Echoes the last user message back."""
        last = body.get("messages", [])[-1]
        return f"echo: {last.get('content', '')}"
'''

FILTER_FILE = '''
class Filter:
    def inlet(self, body: dict, __user__=None) -> dict:
        body["messages"][-1]["content"] += " [filtered]"
        return body
'''


def _plugin(tmp_path, name, source):
    d = tmp_path / "plugins"
    d.mkdir(exist_ok=True)
    (d / name).write_text(textwrap.dedent(source))
    return owc.discover_plugins(str(tmp_path))


def test_tools_methods_become_tooldefs(tmp_path):
    plugins = _plugin(tmp_path, "weather.py", TOOLS_FILE)
    assert len(plugins) == 1 and plugins[0].kind == "tools"
    defs = {d.name: d for d in owc.plugin_tool_defs(plugins, str(tmp_path))}
    assert list(defs) == ["tools_get_weather", "tools_search_notes"]
    weather = defs["tools_get_weather"]
    assert weather.parameters["properties"]["city"]["type"] == "string"
    assert "city" in weather.parameters["required"]
    assert "Get current weather" in weather.description
    # running it: kwargs path
    result = weather.run({"city": "Rome", "units": "imperial"}, None)
    assert json.loads(result.text)["temp"] == 21
    # user-input slot filled from user's message
    notes = defs["tools_search_notes"]
    assert "user hello" in notes.run({"query": "q", "__user__": "hello"}, None).text


def test_pipe_becomes_single_tool(tmp_path):
    plugins = _plugin(tmp_path, "echo.py", PIPE_FILE)
    defs = owc.plugin_tool_defs(plugins, str(tmp_path))
    assert len(defs) == 1 and defs[0].name == "pipe_echo_pipe"
    result = defs[0].run({"messages": [{"role": "user", "content": "hi"}]}, None)
    assert result.text == "echo: hi"


def test_filter_inlet_rewrites_text(tmp_path):
    plugins = _plugin(tmp_path, "filt.py", FILTER_FILE)
    text, notes = owc.apply_inlet(plugins, "hello")
    assert text == "hello [filtered]"
    assert notes == []


def test_broken_plugin_does_not_kill_discovery(tmp_path):
    d = tmp_path / "plugins"
    d.mkdir(exist_ok=True)
    (d / "bad.py").write_text("raise RuntimeError('boom')\n")
    (d / "good.py").write_text(textwrap.dedent(PIPE_FILE))
    plugins = owc.discover_plugins(str(tmp_path))
    assert [p.kind for p in plugins] == ["pipe"]


def test_private_and_lifecycle_methods_skipped():
    from hexiel.tools import ToolDef
    import types
    namespace = {"__annotations__": {}}
    class Tools:
        def startup(self) -> None:
            """lifecycle"""
        def _hidden(self) -> str:
            """private"""
    plugin = owc.LoadedPlugin("x", "Tools", "tools", Tools())
    assert owc.plugin_tool_defs([plugin], "/tmp") == []