"""MCP robustness: a server that dies or hangs is restarted and retried once (backed off after),
per-server call timeouts are honored from config, and a bad timeout value can't disable a server."""

import json
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel.mcp_client import (MCPError, RESTART_COOLDOWN, Server, ServerConfig,
                               _from_entry, start_servers)  # start_servers used by callers of these tools

SLOW_SERVER = textwrap.dedent(r'''
    import json, sys
    sys.stdin.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"serverInfo": {"name": "robust"}}}), flush=True)
        elif method == "tools/list":
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
                "tools": [{"name": "do", "description": "", "inputSchema": {"type": "object"}}]}}), flush=True)
        elif method == "tools/call":
            args = (msg.get("params", {}).get("arguments") or {})
            if args.get("slow"):
                time.sleep(60)
            if args.get("die"):
                sys.exit(4)
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "ok"}]}}), flush=True)
''')


def server_for(tmp_path: Path, **overrides) -> Server:
    script = tmp_path / "fake_server.py"
    script.write_text(SLOW_SERVER)
    cfg = ServerConfig(name="fake", command=sys.executable, args=[str(script)], **{"enabled": True, **overrides})
    return Server(cfg).start()


def test_a_hung_call_restarts_the_server_and_recovers(tmp_path):
    srv = server_for(tmp_path, timeout=0.8)
    assert not srv.error
    old_pid = srv.transport.proc.pid
    t0 = time.time()
    with pytest.raises(MCPError) as exc:
        srv.call("do", {"slow": True})            # hangs; twice past the timeout; then backed off
    assert "backed off" in str(exc.value) and time.time() - t0 < 12
    assert srv.restarts == 1 and srv.transport.proc.pid != old_pid     # the transport was replaced
    srv.cooldown_until = 0.0                       # later (after the cooldown): the restarted server serves
    assert srv.call("do", {})["content"][0]["text"] == "ok"


def test_a_server_that_dies_mid_flight_is_restarted_once_then_backed_off(tmp_path):
    srv = server_for(tmp_path, timeout=2)
    with pytest.raises(MCPError):
        srv.call("do", {"die": True})              # the process exits; the restart cannot help either
    assert srv.restarts == 1 and srv.cooldown_until > time.time()
    deadline = srv.cooldown_until
    with pytest.raises(MCPError) as exc:
        srv.call("do", {})                         # inside the cooldown: fail fast, no proc churn
    assert "backed off" in str(exc.value)
    assert srv.restarts == 1 and srv.cooldown_until == deadline


def test_a_call_inside_the_cooldown_is_refused_with_the_server_s_health(tmp_path):
    srv = server_for(tmp_path)
    srv.cooldown_until = time.time() + 60
    with pytest.raises(MCPError) as exc:
        srv.call("do", {})
    assert "down" in str(exc.value) and "backed off" in str(exc.value)


def test_timeout_config_is_per_server_and_tolerates_rubbish():
    good = _from_entry("s", {"command": "x", "timeout": "2.5"}, "config.toml")
    bad = _from_entry("s", {"command": "x", "timeout": "banana"}, "config.toml")
    none = _from_entry("s", {"command": "x"}, "config.toml")
    assert good.timeout == 2.5 and bad.timeout == 0.0 and none.timeout == 0.0