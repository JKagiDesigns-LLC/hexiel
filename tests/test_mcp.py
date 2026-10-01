"""MCP client: config loading (.mcp.json + config.toml), stdio handshake,
tool listing/allowlist, calls, image parts, errors. Uses a tiny Python MCP
server so the suite needs no Node; the official reference server was also
verified manually over stdio and streamable HTTP."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import mcp_client as M
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext

SERVER = r'''
import json, sys
TOOLS = [
  {"name": "add", "description": "add two numbers", "annotations": {"readOnlyHint": True},
   "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}},
  {"name": "pic", "description": "returns an image", "inputSchema": {"type": "object"}},
  {"name": "boom", "description": "fails", "inputSchema": {"type": "object"}},
]
print("server banner on stdout should be ignored", flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    m, p = msg["method"], msg.get("params", {})
    if m == "initialize":
        res = {"protocolVersion": p["protocolVersion"], "capabilities": {"tools": {}},
               "serverInfo": {"name": "tiny", "version": "1"}}
    elif m == "tools/list":
        res = {"tools": TOOLS}
    elif m == "tools/call" and p["name"] == "add":
        res = {"content": [{"type": "text", "text": str(p["arguments"]["a"] + p["arguments"]["b"])}]}
    elif m == "tools/call" and p["name"] == "pic":
        res = {"content": [{"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"},
                           {"type": "text", "text": "a picture"}]}
    elif m == "tools/call":
        res = {"content": [{"type": "text", "text": "it broke"}], "isError": True}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "nope"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}), flush=True)
'''


def tiny_server(tmp_path) -> str:
    path = tmp_path / "tiny_mcp.py"
    path.write_text(SERVER)
    return str(path)


def test_configs_from_mcp_json_and_config_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TINY_TOKEN", "s3cret")
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "files": {"command": "npx", "args": ["-y", "server-files", "${PWD:-/x}"]},
        "remote": {"type": "http", "url": "https://mcp.example/mcp",
                   "headers": {"Authorization": "Bearer ${TINY_TOKEN}"}},
        "off": {"command": "x", "enabled": False}}}))
    toml_mcp = {"servers": {"files": {"command": "uvx", "args": ["files-server"], "tools": ["read"]}}}
    cfgs = {c.name: c for c in M.load_configs(str(tmp_path), toml_mcp)}
    assert set(cfgs) == {"files", "remote"}                      # disabled one dropped
    assert cfgs["files"].command == "uvx" and cfgs["files"].tools == ["read"]   # config.toml wins
    assert cfgs["remote"].headers["Authorization"] == "Bearer s3cret"           # ${VAR} expanded


def test_stdio_server_tools_and_calls(tmp_path):
    cfg = M.ServerConfig(name="tiny", command=sys.executable, args=[tiny_server(tmp_path)])
    (server,) = M.start_servers([cfg])
    assert not server.error and server.info["name"] == "tiny"
    defs = {d.name: d for d in M.tool_defs([server])}
    assert set(defs) == {"mcp__tiny__add", "mcp__tiny__pic", "mcp__tiny__boom"}
    assert defs["mcp__tiny__add"].readonly and not defs["mcp__tiny__pic"].readonly
    ctx = ToolContext(cwd=str(tmp_path), chain=Toolchain())
    assert defs["mcp__tiny__add"].run({"a": 2, "b": 40}, ctx).text == "42"
    pic = defs["mcp__tiny__pic"].run({}, ctx)
    assert pic.images and pic.images[0]["media_type"] == "image/png" and pic.text == "a picture"
    boom = defs["mcp__tiny__boom"].run({}, ctx)
    assert boom.is_error and "it broke" in boom.text
    server.close()


def test_allowlist_and_failed_server(tmp_path):
    good = M.ServerConfig(name="tiny", command=sys.executable, args=[tiny_server(tmp_path)], tools=["add"])
    bad = M.ServerConfig(name="gone", command=str(tmp_path / "no-such-server"))
    servers = M.start_servers([good, bad])
    assert [d.name for d in M.tool_defs(servers)] == ["mcp__tiny__add"]
    status = "\n".join(M.status_lines(servers))
    assert "✓ tiny" in status and "allowlisted" in status and "✗ gone" in status
    servers[0].close()


def test_add_preset_merges_into_mcp_json(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"mine": {"command": "x"}}}))
    cfg, note = M.add_preset("supabase", str(tmp_path))
    data = json.loads((tmp_path / ".mcp.json").read_text())
    assert set(data["mcpServers"]) == {"mine", "supabase"} and "SUPABASE_ACCESS_TOKEN" in note
    cfg, _ = M.add_preset("playwright", str(tmp_path))
    assert cfg.tools and "browser_navigate" in cfg.tools          # lean allowlist by default
