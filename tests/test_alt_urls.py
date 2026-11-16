"""A model server with two addresses (home LAN, then Tailscale): the first one that answers is used."""

import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import llm
from hexiel.config import ModelConfig


def listener():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


def test_the_first_reachable_address_wins(monkeypatch):
    llm._reach.clear()
    s, port = listener()
    try:
        dead = "http://127.0.0.1:9/v1"
        live = f"http://127.0.0.1:{port}/v1"
        assert llm.pick_base_url(dead, [live]) == live            # away from home: the LAN address is dead
        assert llm.pick_base_url(live, [dead]) == live            # at home: the main one answers
    finally:
        s.close()


def test_nothing_answers_keeps_the_main_address_for_the_error():
    llm._reach.clear()
    assert llm.pick_base_url("http://127.0.0.1:9/v1", ["http://127.0.0.1:8/v1"]) == "http://127.0.0.1:9/v1"


def test_no_alternates_costs_nothing(monkeypatch):
    monkeypatch.setattr(llm, "_reachable", lambda *a, **k: (_ for _ in ()).throw(AssertionError("probed")))
    assert llm.pick_base_url("http://x:1/v1/") == "http://x:1/v1"


def test_the_provider_re_picks_each_request_and_profiles_accept_a_string():
    mc = ModelConfig(name="g", provider="openai", model="m", base_url="http://127.0.0.1:9/v1", alt_base_urls="http://127.0.0.1:8/v1")
    assert mc.alt_base_urls == ("http://127.0.0.1:8/v1",)
    p = llm.OpenAICompatProvider("", mc.base_url, mc.alt_base_urls)
    assert p.alts and p.main_url == "http://127.0.0.1:9/v1"


def test_tailscale_addresses_count_as_your_own_machines():
    assert llm._is_local_url("http://100.94.106.25:11436/v1") and llm._is_local_url("http://gmktec.tailec2240.ts.net:11436/v1")
    assert not llm._is_local_url("https://api.deepinfra.com/v1/openai")


def test_stream_tolerates_null_tool_calls():
    """Some OpenAI-compatible servers stream `"tool_calls": null` — that crashed turns with a NoneType error."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from hexiel.llm import OpenAICompatProvider
    chunks = [{"choices": [{"delta": {"content": "hi", "tool_calls": None, "reasoning": None}}]},
              {"choices": None}, {"choices": [{"delta": None, "finish_reason": "stop"}]}]

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length", 0)))
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        p = OpenAICompatProvider("", f"http://127.0.0.1:{srv.server_port}/v1")
        events = list(p.stream([{"role": "user", "content": "x"}], "sys", [], model="m", max_output_tokens=50, extra_body={}, timeout=10))
    finally:
        srv.shutdown()
    assert ("text", "hi") in events
