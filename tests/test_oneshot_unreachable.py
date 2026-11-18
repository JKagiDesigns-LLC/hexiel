"""`hexiel -p` against a model server that is not there must fail with a sentence, not wait and retry for half an hour."""

from hexiel import llm
from hexiel.config import ModelConfig, Settings
from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.tools import build_tools


def make(oneshot: bool) -> Session:
    s = Session(ModelConfig(name="t", provider="openai", model="m", base_url="http://127.0.0.1:9/v1"),
                Settings(True), ".", build_tools(Toolchain()), "x")
    if oneshot:
        s.__dict__["_oneshot"] = True
    return s


def test_oneshot_does_not_wait_for_an_unreachable_server():
    assert make(True)._limit_wait(llm.TransientError("connection refused"), 0) is None


def test_interactive_session_still_waits_and_retries():
    assert make(False)._limit_wait(llm.TransientError("connection refused"), 0) is not None


def test_oneshot_still_waits_out_a_real_rate_limit():
    assert make(True)._limit_wait(llm.RateLimitError("slow down", retry_after=30), 0) is not None
