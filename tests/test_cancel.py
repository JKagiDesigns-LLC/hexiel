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
    chunk = b'data: {"choices":[{"index":0,"delta":{"reasoning":"hmm "}}]}\n\n'

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
                data = self.chunk
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


class ToolCallServer(ThinkingServer):
    """Streams the arguments of one huge tool call (e.g. writing a big file): the client shows nothing
    while this happens, so there are no text/thinking events to check the stop flag on."""
    chunk = (b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"c1",'
             b'"function":{"name":"write","arguments":"xxxxxxxxxxxxxxxx"}}]}}]}\n\n')


def test_stop_while_a_long_tool_call_is_being_written():
    server = ToolCallServer()
    s = make_session()
    s.settings.request_timeout = 60
    provider = llm.OpenAICompatProvider("", f"http://127.0.0.1:{server.port}/v1")
    s.messages.append({"role": "user", "content": "write a big file"})
    threading.Timer(1.0, s.stop).start()
    t0 = time.time()
    s._stream_once(provider, [])
    assert time.time() - t0 < 4
    assert server.sent > 5 and server.hung_up.wait(3)


# ---------------------------------------------------------------- cancelling a running command

def _ctx(tmp_path, stop):
    from hexiel.tools import ToolContext
    from hexiel.tooling import Toolchain
    return ToolContext(cwd=str(tmp_path), chain=Toolchain(), stop_event=stop)


def _alive(marker: str) -> bool:
    import subprocess
    return subprocess.run(["pgrep", "-f", marker], capture_output=True).returncode == 0


def test_cancel_kills_a_long_running_bash_command_and_its_children(tmp_path):
    from hexiel.tools import _run_bash
    stop = threading.Event()
    marker = "sleep 31337"                      # a child process we can look for afterwards
    threading.Timer(0.6, stop.set).start()
    t0 = time.time()
    out = _run_bash({"command": f"{marker} && echo ALL-DONE-MARKER", "timeout": 120}, _ctx(tmp_path, stop))
    assert time.time() - t0 < 3
    assert out.is_error and "cancelled by the user" in out.text and "ALL-DONE-MARKER" not in out.text
    time.sleep(0.3)
    assert not _alive(marker)                   # the sleep itself was killed, not just the shell


def test_cancel_keeps_the_output_produced_so_far(tmp_path):
    from hexiel.tools import _run_bash
    stop = threading.Event()
    threading.Timer(0.8, stop.set).start()
    out = _run_bash({"command": "echo partial-result; sleep 30"}, _ctx(tmp_path, stop))
    assert "partial-result" in out.text and "cancelled" in out.text


def test_a_timeout_parks_the_command_instead_of_killing_it(tmp_path):
    """Timeouts park the command as a background task (it keeps running);
    only the tasks tool's kill ends it — including its children."""
    from hexiel.tools import _run_bash, _TASKS, _tasks_tool
    ctx = _ctx(tmp_path, threading.Event())
    t0 = time.time()
    out = _run_bash({"command": "sleep 31338 && echo no", "timeout": 1}, ctx)
    assert out.is_error is False and "parked as background task" in out.text and time.time() - t0 < 4
    tid = next(w for w in out.text.split() if w.startswith("bg-"))
    time.sleep(0.3)
    assert _alive("sleep 31338")               # parked: the command (and children) still run
    killed = _tasks_tool({"op": "kill", "id": tid}, ctx)
    assert killed.is_error is False and "killed" in killed.text
    for _ in range(20):
        if _TASKS[tid].done:
            break
        time.sleep(0.1)
    time.sleep(0.3)
    assert not _alive("sleep 31338")           # the kill takes the whole process group down


def test_commands_without_cancel_still_work_normally(tmp_path):
    from hexiel.tools import _run_bash
    ok = _run_bash({"command": "echo hi; echo oops >&2; exit 3"}, _ctx(tmp_path, threading.Event()))
    assert "hi" in ok.text and "oops" in ok.text and "exit code 3" in ok.text and ok.is_error


def test_session_wires_its_stop_flag_into_the_tool_context(tmp_path):
    s = make_session()
    ctx = _ctx(tmp_path, None)
    s.bind_tool_ctx(ctx)
    assert ctx.stop_event is s._stop
