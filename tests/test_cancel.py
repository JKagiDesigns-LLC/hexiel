"""Ctrl+C must work while the model server is silent (e.g. reading a long prompt), and must cut the
connection so the server stops working on the abandoned request."""

import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import llm
from hexiel.config import ModelConfig, Settings
from hexiel.session import Session


class SilentServer:
    """Accepts a request and never answers; records when the client hangs up."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.hung_up = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        conn, _ = self.sock.accept()
        conn.settimeout(15)
        try:
            while True:
                if conn.recv(65536) == b"":        # request bytes are ignored; EOF = the client hung up
                    self.hung_up.set()
                    return
        except OSError:
            pass


def make_session():
    mc = ModelConfig(name="t", provider="openai", model="fake", base_url="http://x", context_window=10000)
    s = Session(mc, Settings(True), cwd="/tmp", tools=[], system_prompt="system")
    s.on_event = lambda *a, **k: None
    return s


def test_stop_interrupts_a_stream_that_is_blocked_waiting():
    s = make_session()
    block = threading.Event()

    def stuck():
        block.wait(30)
        yield ("text", "late")

    class P:
        pass

    threading.Timer(0.5, s.stop).start()
    t0 = time.time()
    assert list(s._interruptible(stuck(), P())) == []
    assert time.time() - t0 < 3
    block.set()


def test_abort_cuts_the_connection_and_wakes_the_blocked_request():
    server = SilentServer()
    provider = llm.OpenAICompatProvider("", f"http://127.0.0.1:{server.port}/v1")
    errors = []

    def run():
        try:
            list(provider.stream([{"role": "user", "content": "hi"}], "sys", [], model="m",
                                 max_output_tokens=10, extra_body={}, timeout=30))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    time.sleep(0.7)
    assert worker.is_alive()                       # blocked: the server never answers
    provider.abort()
    worker.join(5)
    assert not worker.is_alive() and errors and isinstance(errors[0], httpx.TransportError)
    assert server.hung_up.wait(3)                  # the server saw the disconnect, so it can stop working


def test_stop_during_a_silent_request_returns_quickly_end_to_end():
    server = SilentServer()
    s = make_session()
    s.model_config.base_url = f"http://127.0.0.1:{server.port}/v1"
    s.settings.request_timeout = 60
    provider = llm.OpenAICompatProvider("", s.model_config.base_url)
    s.messages.append({"role": "user", "content": "hi"})
    threading.Timer(0.7, s.stop).start()
    t0 = time.time()
    s._stream_once(provider, [])
    assert time.time() - t0 < 4
    assert server.hung_up.wait(3)


class ThinkingServer:
    """Streams reasoning tokens forever, like a model in the middle of a long thought."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.hung_up = threading.Event()
        self.sent = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        conn, _ = self.sock.accept()
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
        try:
            while True:
                data = b'data: {"choices":[{"index":0,"delta":{"reasoning":"hmm "}}]}\n\n'
                conn.sendall(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.sent += 1
                time.sleep(0.05)
        except OSError:
            self.hung_up.set()


def test_stop_in_the_middle_of_a_thought_returns_quickly_and_hangs_up():
    server = ThinkingServer()
    s = make_session()
    s.settings.request_timeout = 60
    provider = llm.OpenAICompatProvider("", f"http://127.0.0.1:{server.port}/v1")
    s.messages.append({"role": "user", "content": "think hard"})
    thoughts = []
    s.on_event = lambda event, *p, **k: thoughts.append(event) if event == "thinking" else None
    threading.Timer(1.0, s.stop).start()
    t0 = time.time()
    s._stream_once(provider, [])
    assert time.time() - t0 < 4
    assert len(thoughts) > 5                       # it really was mid-thought when stopped
    assert server.hung_up.wait(3)                  # and the connection was cut, so the server stops generating
