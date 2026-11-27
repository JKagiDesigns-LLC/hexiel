"""A tool call a model writes as text never reaches a screen, however the stream is chunked."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import leaks

PROSE = "Now let me see the test file's style first.\n\n"
DSML = '<｜DSML｜parameter name="command" string="true">cd /x; sed -n \'90,155p\' T.java</｜DSML｜parameter>'
CASES = [
    (PROSE + DSML, PROSE.rstrip()),
    (PROSE + '<｜DSML｜function_calls>\n<｜DSML｜invoke name="bash">' + DSML, PROSE.rstrip()),
    (PROSE + DSML.replace("｜", "|"), PROSE.rstrip()),                      # ASCII bars
    (PROSE + "< | DSML | parameter name=\"command\">ls", PROSE.rstrip()),    # spaces, as a gateway rendered it
    (DSML, ""),                                                              # nothing but the leak
    ("Reading it.\n[tool call] bash({\"command\": \"ls\"})", "Reading it."),
    ("plain answer, no tool", "plain answer, no tool"),
    ("x < y and a[0] is [not a call] <b>bold</b> and 5 < 6 |", "x < y and a[0] is [not a call] <b>bold</b> and 5 < 6 |"),
    ("ends with a lone <", "ends with a lone <"),
    ("ends with [tool", "ends with [tool"),
    ("a [tool call] mentioned in prose is fine", "a [tool call] mentioned in prose is fine"),
]


def run(chunks):
    f = leaks.LeakFilter()
    out = "".join(f.feed(c) for c in chunks) + f.flush()
    return out.rstrip()


@pytest.mark.parametrize("text,shown", CASES)
def test_whole_reply_in_one_chunk(text, shown):
    assert run([text]) == shown


@pytest.mark.parametrize("text,shown", CASES)
def test_one_character_at_a_time(text, shown):
    assert run(list(text)) == shown


@pytest.mark.parametrize("text,shown", CASES)
def test_every_two_way_split(text, shown):
    for i in range(len(text) + 1):
        assert run([text[:i], text[i:]]) == shown, f"split at {i}"


def test_nothing_after_the_leak_is_shown_even_if_prose_follows():
    assert run([PROSE, DSML, " and then some more words"]) == PROSE.rstrip()


def test_strip_and_detect():
    assert leaks.has_leak(PROSE + DSML) and not leaks.has_leak("a <b> and [tool]")
    assert leaks.strip_leaks(PROSE + DSML) == PROSE.rstrip()
    assert leaks.strip_leaks("fine") == "fine"


# ------------------------------------------------------------ through a real session's stream

def _session(tmp_path, monkeypatch, replies):
    """A session whose provider streams the given replies in 7-character chunks; tools really run."""
    from hexiel.config import ModelConfig, Settings
    from hexiel.session import Session
    from hexiel.tools import Toolchain, ToolContext, build_tools
    tools = build_tools(Toolchain())
    s = Session(ModelConfig(name="m", provider="openai", model="m", base_url="http://127.0.0.1:9/v1"),
                Settings(auto_approve=True), str(tmp_path), tools, "sys")
    s.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    s.save = lambda: None
    events = []
    s.on_event = lambda e, *p, **k: events.append((e, *p))
    seq = iter(replies)

    class Provider:
        def stream(self, *a, **k):
            text = next(seq)
            for i in range(0, len(text), 7):
                yield ("text", text[i:i + 7])
            yield ("assistant", {"role": "assistant", "content": text})
            yield ("done", None)

    monkeypatch.setattr("hexiel.llm.make_provider", lambda *a, **k: Provider())
    return s, events


def shown(events):
    return "".join(e[1] for e in events if e[0] == "text")


def test_a_leaked_call_is_never_displayed_but_still_runs(tmp_path, monkeypatch):
    s, events = _session(tmp_path, monkeypatch, [
        "Let me look.\n\n" + '<｜DSML｜parameter name="command" string="true">echo hello-from-leak', "All done."])
    s.run_turn("go")
    text = shown(events)
    assert "DSML" not in text and "parameter" not in text and "echo hello-from-leak" not in text
    assert "Let me look." in text and "All done." in text
    results = [m["content"] for m in s.messages if m.get("role") == "tool"]
    assert results and "hello-from-leak" in results[0]                        # the call really ran
    assert not any(leaks.has_leak(str(m.get("content", ""))) for m in s.messages if m.get("role") == "assistant")


def test_an_unreadable_leaked_call_is_hidden_and_the_model_is_asked_to_retry(tmp_path, monkeypatch):
    s, events = _session(tmp_path, monkeypatch, [
        'Trying.\n<｜DSML｜parameter name="nonsense_arg" string="true">x', "Recovered."])
    s.run_turn("go")
    assert "DSML" not in shown(events) and "nonsense_arg" not in shown(events)
    assert any("could not be read" in str(e[1]) for e in events if e[0] == "note")
    assert s.messages[-1]["content"] == "Recovered."
    assert not any(leaks.has_leak(str(m.get("content", ""))) for m in s.messages if m.get("role") == "assistant")


def test_old_saved_chats_render_clean(tmp_path, monkeypatch):
    s, _ = _session(tmp_path, monkeypatch, [])
    s.messages = [{"role": "user", "content": "hi"},
                  {"role": "assistant", "content": PROSE + DSML}]
    from hexiel.webui import WebState
    items = WebState(s).render_history()
    texts = [i["text"] for i in items if i.get("t") == "assistant"]
    assert texts == [PROSE.rstrip()]
