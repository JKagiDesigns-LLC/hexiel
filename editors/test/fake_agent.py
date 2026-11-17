"""A stand-in for `hexiel --acp` that speaks just enough of the Agent Client Protocol to test the editor plugins
without a model: it streams a reply (quoting the prompt), runs a tool that needs approval, and shows a diff."""
import json
import sys


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def update(sid, u):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": u}})


for line in sys.stdin:
    msg = json.loads(line)
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1, "agentInfo": {"name": "fake", "version": "0"},
                                                        "agentCapabilities": {}, "authMethods": []}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "s1"}})
    elif method == "session/prompt":
        text = " ".join(b.get("text", "") for b in params["prompt"] if b.get("type") == "text")
        update("s1", {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "You said: "}})
        update("s1", {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})
        update("s1", {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "edit a.txt", "kind": "edit", "status": "in_progress"})
        send({"jsonrpc": "2.0", "id": "perm1", "method": "session/request_permission", "params": {
            "sessionId": "s1", "toolCall": {"toolCallId": "c1", "title": "edit a.txt", "kind": "edit", "status": "pending"},
            "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "reject", "name": "Reject", "kind": "reject_once"}]}})
        verdict = json.loads(sys.stdin.readline())             # the editor's answer to the permission request
        chosen = ((verdict.get("result") or {}).get("outcome") or {}).get("optionId", "cancelled")
        update("s1", {"sessionUpdate": "tool_call_update", "toolCallId": "c1",
                      "status": "completed" if chosen == "allow" else "failed",
                      "content": [{"type": "diff", "path": "/tmp/a.txt", "oldText": "one\n", "newText": "two\n"}]})
        update("s1", {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": f"\nchoice={chosen}"}})
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
    elif method == "session/cancel":
        pass
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "method not found"}})
