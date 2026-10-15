"""hexiel's self-test suite: tools, llm conversion, todo, compaction, gitlab parsing.

These tests are what the `test` tool and /heal run against — keep them green.
"""

import struct

from hexiel import todo as todo_mod
from hexiel.gitlab import _parse_remote
from hexiel.llm import AssistantBuffer, AnthropicProvider, OpenAICompatProvider, est_tokens
from hexiel.session import Session
from hexiel.config import ModelConfig, Settings
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, build_tools, parse_image_size


def make_ctx(tmp_path) -> ToolContext:
    return ToolContext(cwd=str(tmp_path), chain=Toolchain())


def tool_by_name(name):
    registry = {t.name: t for t in build_tools(Toolchain())}
    return registry[name]


# ---------------------------------------------------------------- tools

def test_write_read_edit_roundtrip(tmp_path):
    ctx = make_ctx(tmp_path)
    by = {t.name: t for t in build_tools(Toolchain())}
    by["write"].run({"path": str(tmp_path / "f.txt"), "content": "hello\nworld"}, ctx)
    read = by["read"].run({"path": str(tmp_path / "f.txt")}, ctx)
    assert "hello" in read.text and "world" in read.text
    by["edit"].run({"path": str(tmp_path / "f.txt"),
                    "old_string": "hello", "new_string": "bonjour"}, ctx)
    assert "bonjour" in (tmp_path / "f.txt").read_text()
    assert "hello" not in (tmp_path / "f.txt").read_text()


def test_edit_requires_unique_match(tmp_path):
    ctx = make_ctx(tmp_path)
    by = {t.name: t for t in build_tools(Toolchain())}
    by["write"].run({"path": str(tmp_path / "f.txt"), "content": "dup dup"}, ctx)
    result = by["edit"].run({"path": str(tmp_path / "f.txt"), "old_string": "dup",
                             "new_string": "one"}, ctx)
    assert result.is_error
    result = by["edit"].run({"path": str(tmp_path / "f.txt"), "old_string": "dup",
                             "new_string": "one", "replace_all": True}, ctx)
    assert not result.is_error
    assert (tmp_path / "f.txt").read_text() == "one one"


def test_glob_grep_and_list(tmp_path):
    ctx = make_ctx(tmp_path)
    by = {t.name: t for t in build_tools(Toolchain())}
    (tmp_path / "a.py").write_text("def find_me():\n    pass\n")
    (tmp_path / "b.md").write_text("docs\n")
    hits = by["grep"].run({"pattern": "find_me", "path": str(tmp_path)}, ctx)
    assert "a.py" in hits.text
    globs = by["glob"].run({"pattern": "*.md", "path": str(tmp_path)}, ctx)
    assert "b.md" in globs.text
    listing = by["list"].run({"path": str(tmp_path)}, ctx)
    assert "a.py" in listing.text and "b.md" in listing.text


def test_image_probe(tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 8 + struct.pack(">II", 640, 480)
    width, height, fmt = parse_image_size(png)
    assert (width, height, fmt) == (640, 480, "png")


def test_web_fetch_rejects_bad_url():
    from hexiel.tools import _web_fetch
    result = _web_fetch({"url": "definitely-not-a-real-host-xyz.invalid"}, None)
    assert result.is_error


# ---------------------------------------------------------------- llm

def test_assistant_buffer_accumulates_json_args():
    buf = AssistantBuffer()
    buf.start_call(0, "c1", "bash")
    buf.add_args(0, '{"comm')
    buf.add_args(0, 'and": "ls"}')
    msg = buf.finalize()
    assert msg["tool_calls"][0]["arguments"]["command"] == "ls"


def test_anthropic_tool_result_with_image():
    msgs = AnthropicProvider._convert_messages(
        [{"role": "tool", "tool_call_id": "t1", "content": "x",
          "images": [{"media_type": "image/png", "data": "AAAA"}]}])
    blocks = msgs[0]["content"]
    assert blocks[0]["type"] == "tool_result" and blocks[1]["type"] == "image"


def test_openai_tool_image_injected_as_user_part():
    msgs = OpenAICompatProvider._convert_messages(
        [{"role": "tool", "tool_call_id": "t1", "content": "x",
          "images": [{"media_type": "image/png", "data": "AAAA"}]}])
    assert msgs[1]["content"][0]["type"] == "image_url"


def test_openai_tool_calls_roundtrip():
    msgs = OpenAICompatProvider._convert_messages(
        [{"role": "assistant", "content": "working",
          "tool_calls": [{"id": "c1", "name": "list", "arguments": {"path": "/tmp"}}]}])
    assert msgs[0]["tool_calls"][0]["function"]["name"] == "list"
    assert '"path": "/tmp"' in msgs[0]["tool_calls"][0]["function"]["arguments"].replace("'", '"')
    assert est_tokens("x" * 40) == 10


# ---------------------------------------------------------------- todo

def test_todo_create_update_resume(tmp_path):
    assert todo_mod.action_create(
        {"title": "fix widget", "steps": ["write test", "patch code", "run tests"]},
        str(tmp_path)).startswith("todo.md created")
    assert todo_mod.action_update({"step": "write test", "state": "~"}, str(tmp_path))
    assert todo_mod.action_update({"step": "write test", "state": "x"}, str(tmp_path))
    title, left_off, items = todo_mod.read_todos(str(tmp_path))
    assert title == "fix widget" and items[0].state == "x"
    assert todo_mod.summary_line(str(tmp_path)).startswith("todo 1/3")


def test_todo_in_progress_is_unique(tmp_path):
    todo_mod.action_create({"title": "t", "steps": ["a", "b", "c"]}, str(tmp_path))
    todo_mod.action_update({"step": "a", "state": "~"}, str(tmp_path))
    todo_mod.action_update({"step": "b", "state": "~"}, str(tmp_path))
    _, _, items = todo_mod.read_todos(str(tmp_path))
    marked_in_progress = [i for i in items if i.state == "~"]
    assert len(marked_in_progress) == 1
    assert marked_in_progress[0].text == "b"


# ---------------------------------------------------------------- compaction

def test_compaction_replaces_history_with_summary():
    mc = ModelConfig(name="t", provider="openai", model="fake", base_url="http://x",
                     context_window=1000)
    session = Session(mc, Settings(True), cwd="/tmp", tools=[], system_prompt="system")
    for i in range(6):
        session.messages.append({"role": "user", "content": f"request {i} " + "w" * 200})
        session.messages.append({"role": "assistant", "content": f"did {i}"})

    class FakeProvider:
        def stream(self, messages, system, tools, **kw):
            assert tools == [] and system
            yield ("text", "SUMMARY of earlier work.")
            yield ("done", {})
            yield ("assistant", None)

    session.on_event = lambda *a, **k: None
    session.compact(FakeProvider(), manual=True)
    joined = "\n".join(m.get("content", "") for m in session.messages)
    assert "SUMMARY" in joined and "auto-compacted" in joined
    assert "did 0" not in joined          # summarized away
    assert "did 5" in joined              # recent turns kept verbatim


def test_estimate_tokens_counts_images_and_calls():
    session = Session(ModelConfig(name="t", provider="openai", model="f",
                                  base_url="http://x"),
                      Settings(True), cwd="/tmp", tools=[], system_prompt="abc")
    session.messages.append({"role": "assistant", "content": "hi",
                             "tool_calls": [{"id": "1", "name": "bash", "arguments": {"command": "x" * 40}}]})
    session.messages.append({"role": "tool", "tool_call_id": "1", "content": "y",
                             "images": [{"media_type": "image/png", "data": "A" * 100,
                                         "approx_tokens": 900}]})
    total = session.estimate_tokens()
    assert total > 950  # image tokens dominate


# ---------------------------------------------------------------- gitlab

def test_parse_remote_forms():
    assert _parse_remote("git@gitlab.com:group/sub/repo.git") == ("gitlab.com", "group/sub/repo")
    assert _parse_remote("ssh://git@git.example.com:2222/team/proj.git") == ("git.example.com", "team/proj")
    assert _parse_remote("https://gitlab.com/team/proj/") == ("gitlab.com", "team/proj")
    assert _parse_remote("file:///local/only") == ("", "")

# ---------------------------------------------------------------- token economy

def test_tool_result_cache_repeats_cost_once(tmp_path):
    from hexiel.config import ModelConfig, Settings
    ctx = make_ctx(tmp_path)
    registry = {t.name: t for t in build_tools(Toolchain())}
    session = Session(ModelConfig(name="t", provider="openai", model="f",
                                  base_url="http://x"), Settings(True),
                      cwd=str(tmp_path), tools=list(registry.values()), system_prompt="s")
    session.bind_tool_ctx(ctx)
    call = {"id": "c1", "name": "list", "arguments": {"path": str(tmp_path)}}
    session._execute_tool(call)
    call2 = {"id": "c2", "name": "list", "arguments": {"path": str(tmp_path)}}
    session._execute_tool(call2)
    contents = [m["content"] for m in session.messages if m["role"] == "tool"]
    assert len(contents) == 2
    assert "cached" in contents[1]
    assert contents[1].startswith(contents[0].split("\n(cached")[0][:50])


def test_one_huge_turn_compacts_inside_the_turn_and_does_not_loop():
    """Regression: a single agentic turn of ~150 tool calls filled the window, 'the last 1-2 turns' was
    nearly everything, and compaction saved ~2k tokens per pass and re-triggered on every step."""
    mc = ModelConfig(name="t", provider="openai", model="fake", base_url="http://x", context_window=20000)
    session = Session(mc, Settings(True), cwd="/tmp", tools=[], system_prompt="system")
    session.messages.append({"role": "user", "content": "<auto-compaction>\n\nold summary\n</auto-compaction>"})
    session.messages.append({"role": "user", "content": "FIX THE FILTERING BUG please"})
    for i in range(150):
        session.messages.append({"role": "assistant", "content": f"step {i}",
                                 "tool_calls": [{"id": f"c{i}", "name": "bash", "arguments": {"command": "ls"}}]})
        session.messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 400})
    assert session.compaction_needed()
    seen = {}

    class FakeProvider:
        def stream(self, messages, system, tools, **kw):
            seen["chars"] = len(messages[0]["content"])
            yield ("text", "SUMMARY of the work so far.")
            yield ("done", {})
            yield ("assistant", None)

    session.on_event = lambda *a, **k: None
    session.compact(FakeProvider())
    assert not session.compaction_needed()                      # actually freed space, so no loop
    assert session.estimate_tokens() < 20000 * 0.5
    assert session.messages[0]["content"].count("FIX THE FILTERING BUG") == 1      # request kept verbatim
    assert session.messages[1]["role"] == "assistant"          # never starts on an orphaned tool result
    assert session.messages[-1]["content"] == "x" * 400 and session.messages[-2]["content"] == "step 149"
    assert seen["chars"] < 20000 * 0.7 * 4                      # the summarizer prompt fits its window
