"""`hexiel --acp`: the Agent Client Protocol server Zed / JetBrains talk to. Driven here over real pipes with a
scripted model, exactly as an editor would drive it."""

import base64
import json
import os
import queue
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import acp
from hexiel.config import ModelConfig, Settings
from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, ToolDef, text_result

MC = ModelConfig(name="t", provider="openai", model="m", base_url="http://127.0.0.1:9")


class Editor:
    """The client side: sends requests, collects notifications, answers permission requests."""

    def __init__(self, server: acp.Server, to_server, from_server, answer="allow"):
        self.w, self.r = to_server, from_server
        self.updates, self.permissions, self.answer = [], [], answer
        self.responses: dict = {}
        self.cond = threading.Condition()
        self.next_id = 0
        threading.Thread(target=server.serve, daemon=True).start()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for raw in iter(self.r.readline, b""):
            msg = json.loads(raw)
            if msg.get("method") == "session/update":
                self.updates.append(msg["params"]["update"])
            elif msg.get("method") == "session/request_permission":
                self.permissions.append(msg["params"])
                outcome = {"outcome": "selected", "optionId": self.answer} if self.answer else {"outcome": "cancelled"}
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {"outcome": outcome}})
            elif "id" in msg:
                with self.cond:
                    self.responses[msg["id"]] = msg
                    self.cond.notify_all()

    def _send(self, msg):
        self.w.write((json.dumps(msg) + "\n").encode())
        self.w.flush()

    def call(self, method, params, timeout=20):
        self.next_id += 1
        rid = self.next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        with self.cond:
            assert self.cond.wait_for(lambda: rid in self.responses, timeout), f"no answer to {method}"
            return self.responses.pop(rid)

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})


def make(tmp_path, monkeypatch, replies, answer="allow", tools=None):
    ran = []

    def run_bash(args, ctx):
        ran.append(args["command"])
        return text_result(f"ran {args['command']}")
    tools = tools or [ToolDef("bash", "shell", {"type": "object", "properties": {}}, run=run_bash)]
    built = []

    def build(cwd, resumed):
        s = Session(MC, Settings(False), cwd, list(tools), "sys")
        s.bind_tool_ctx(ToolContext(cwd=cwd, chain=Toolchain()))
        s.save = lambda: None
        if resumed:
            s.id, s.messages = resumed["id"], resumed["messages"]
        built.append(s)
        return s
    script = iter(replies)

    def stream_once(self, provider, defs):
        reply = next(script)
        if reply.get("content"):
            self.on_event("text", reply["content"])
        return reply
    monkeypatch.setattr(Session, "_stream_once", stream_once)
    monkeypatch.setattr("hexiel.llm.make_provider", lambda *a, **k: object())
    a_r, a_w = os.pipe()
    b_r, b_w = os.pipe()
    server = acp.Server(open(a_r, "rb"), open(b_w, "wb"), build=build)
    return Editor(server, open(a_w, "wb"), open(b_r, "rb"), answer), ran, built


def test_initialize_advertises_capabilities(tmp_path, monkeypatch):
    ed, _, _ = make(tmp_path, monkeypatch, [])
    res = ed.call("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": True}}})["result"]
    assert res["protocolVersion"] == 1 and res["agentCapabilities"]["loadSession"]
    assert res["agentCapabilities"]["promptCapabilities"]["image"] and res["agentInfo"]["name"] == "hexiel"
    assert ed.call("authenticate", {"methodId": "x"})["result"] == {}
    assert ed.call("no/such", {})["error"]["code"] == -32601


def test_a_prompt_streams_text_tool_calls_and_asks_permission(tmp_path, monkeypatch):
    replies = [{"role": "assistant", "content": "Let me check.", "tool_calls": [
        {"id": "c1", "name": "bash", "arguments": {"command": "ls"}}]}, {"role": "assistant", "content": "Done."}]
    ed, ran, _ = make(tmp_path, monkeypatch, replies)
    sid = ed.call("session/new", {"cwd": str(tmp_path), "mcpServers": []})["result"]["sessionId"]
    res = ed.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "list files"}]})
    assert res["result"] == {"stopReason": "end_turn"} and ran == ["ls"]
    kinds = [u["sessionUpdate"] for u in ed.updates]
    assert kinds.index("tool_call") < kinds.index("tool_call_update")
    text = "".join(u["content"]["text"] for u in ed.updates if u["sessionUpdate"] == "agent_message_chunk")
    assert "Let me check." in text and "Done." in text
    perm = ed.permissions[0]
    call = next(u for u in ed.updates if u["sessionUpdate"] == "tool_call")
    done = next(u for u in ed.updates if u["sessionUpdate"] == "tool_call_update")
    assert perm["toolCall"]["toolCallId"] == call["toolCallId"] == done["toolCallId"]   # one card in the editor
    assert call["kind"] == "execute" and call["title"] == "bash ls" and done["status"] == "completed"
    assert "ran ls" in done["content"][0]["content"]["text"]


def test_rejecting_in_the_editor_means_the_tool_never_runs(tmp_path, monkeypatch):
    replies = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "name": "bash", "arguments": {"command": "rm -rf build"}}]}, {"role": "assistant", "content": "ok"}]
    ed, ran, _ = make(tmp_path, monkeypatch, replies, answer="reject")
    sid = ed.call("session/new", {"cwd": str(tmp_path)})["result"]["sessionId"]
    ed.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "clean"}]})
    assert ran == [] and ed.permissions


def test_auto_mode_skips_permission_requests(tmp_path, monkeypatch):
    replies = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "name": "bash", "arguments": {"command": "make"}}]}, {"role": "assistant", "content": "ok"}]
    ed, ran, _ = make(tmp_path, monkeypatch, replies)
    new = ed.call("session/new", {"cwd": str(tmp_path)})["result"]
    assert new["modes"]["currentModeId"] == "ask"
    assert ed.call("session/set_mode", {"sessionId": new["sessionId"], "modeId": "auto"})["result"] == {}
    ed.call("session/prompt", {"sessionId": new["sessionId"], "prompt": [{"type": "text", "text": "build"}]})
    assert ran == ["make"] and not ed.permissions


def test_cancel_stops_a_running_turn(tmp_path, monkeypatch):
    gate = queue.Queue()

    def slow(args, ctx):
        gate.get(timeout=10)
        return text_result("late")
    tools = [ToolDef("wait", "wait", {"type": "object", "properties": {}}, readonly=True, run=slow)]
    replies = [{"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "wait", "arguments": {}}]},
               {"role": "assistant", "content": "should not be reached"}]
    ed, _, built = make(tmp_path, monkeypatch, replies, tools=tools)
    sid = ed.call("session/new", {"cwd": str(tmp_path)})["result"]["sessionId"]
    threading.Timer(0.5, lambda: (ed.notify("session/cancel", {"sessionId": sid}), gate.put(1))).start()
    res = ed.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "wait"}]})
    assert res["result"]["stopReason"] == "cancelled"


def test_prompt_blocks_become_mentions_and_attachments(tmp_path, monkeypatch):
    ed, _, built = make(tmp_path, monkeypatch, [{"role": "assistant", "content": "seen"}])
    sid = ed.call("session/new", {"cwd": str(tmp_path)})["result"]["sessionId"]
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
    ed.call("session/prompt", {"sessionId": sid, "prompt": [
        {"type": "text", "text": "look at"},
        {"type": "resource_link", "uri": f"file://{tmp_path}/src/app.py", "name": "app.py"},
        {"type": "resource", "resource": {"uri": "file:///x/notes.md", "text": "remember the milk"}},
        {"type": "image", "mimeType": "image/png", "data": png}]})
    sent = next(m["content"] for m in built[0].messages if m["role"] == "user" and "look at" in str(m["content"]))
    sent = sent if isinstance(sent, str) else json.dumps(sent)
    assert f"@{tmp_path}/src/app.py" in sent and "remember the milk" in sent and ".png" in sent


def test_load_replays_a_saved_conversation(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "sessions").mkdir(parents=True)
    (home / "sessions" / "abc123.json").write_text(json.dumps({"id": "abc123", "cwd": str(tmp_path), "model": "t",
        "messages": [{"role": "user", "content": "fix the bug"}, {"role": "assistant", "content": "Fixed it."}]}))
    monkeypatch.setattr("hexiel.config.home_data_dir", lambda: home)
    ed, _, built = make(tmp_path, monkeypatch, [])
    res = ed.call("session/load", {"sessionId": "abc123", "cwd": str(tmp_path), "mcpServers": []})
    assert "error" not in res and built[0].id == "abc123"
    assert [(u["sessionUpdate"], u["content"]["text"]) for u in ed.updates] == [
        ("user_message_chunk", "fix the bug"), ("agent_message_chunk", "Fixed it.")]
    assert ed.call("session/load", {"sessionId": "nope", "cwd": str(tmp_path)})["error"]["code"] == -32602


def test_the_todo_list_is_sent_as_a_plan(tmp_path, monkeypatch):
    from hexiel import todo
    monkeypatch.setattr(todo, "read_todos", lambda cwd: ("t", "", [todo.TodoItem("x", "read code"),
                                                                  todo.TodoItem("~", "write fix")]))
    tools = [ToolDef("todo", "todo", {"type": "object", "properties": {}}, readonly=True,
                     run=lambda a, c: text_result("ok"))]
    replies = [{"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "todo", "arguments": {}}]},
               {"role": "assistant", "content": "ok"}]
    ed, _, _ = make(tmp_path, monkeypatch, replies, tools=tools)
    sid = ed.call("session/new", {"cwd": str(tmp_path)})["result"]["sessionId"]
    ed.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "plan"}]})
    plan = next(u for u in ed.updates if u["sessionUpdate"] == "plan")
    assert [(e["content"], e["status"]) for e in plan["entries"]] == [("read code", "completed"),
                                                                     ("write fix", "in_progress")]


def test_a_call_to_an_unknown_tool_still_gets_a_card_before_its_failure(tmp_path, monkeypatch):
    replies = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "c9", "name": "container.exec", "arguments": {"cmd": "ls"}}]}, {"role": "assistant", "content": "ok"}]
    ed, _, _ = make(tmp_path, monkeypatch, replies)
    sid = ed.call("session/new", {"cwd": str(tmp_path)})["result"]["sessionId"]
    ed.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "go"}]})
    cards = [u for u in ed.updates if u["sessionUpdate"] in ("tool_call", "tool_call_update")]
    assert [c["sessionUpdate"] for c in cards] == ["tool_call", "tool_call_update"]
    assert cards[0]["toolCallId"] == cards[1]["toolCallId"] and cards[1]["status"] == "failed"
