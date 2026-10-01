"""Token-economy tests: local-first trimming must keep outputs complete
where it matters (errors, verdicts) and cheap everywhere else."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, head_tail, html_to_text


def make_ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(cwd=str(tmp_path), chain=Toolchain())


def test_head_tail_keeps_both_ends():
    text = "BOOT-ERROR-HERE\n" + "x" * 50000 + "\nFINAL-OK"
    out = head_tail(text, head=100, tail=100)
    assert "BOOT-ERROR-HERE" in out and "FINAL-OK" in out
    assert "elided" in out and len(out) < 600


def test_head_tail_passthrough_short():
    assert head_tail("short") == "short"


def test_map_builds_tree_and_caches(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text("x = 1\n" * 200)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_auth.py").write_text("def test(): pass\n")
    from hexiel.tools import _map
    ctx = make_ctx(tmp_path)
    out1 = _map({}, ctx)
    assert "src/" in out1.text and "auth.py" in out1.text
    assert "tests/" in out1.text
    # second call: cached result with a note, no rebuild
    out2 = _map({}, ctx)
    assert "cached" in out2.text
    # touching a file invalidates the cache (fingerprint changes)
    (tmp_path / "NEW.py").write_text("fresh")
    out3 = _map({}, ctx)
    assert "cached" not in out3.text and "NEW.py" in out3.text


def test_html_to_text_strips_boilerplate():
    html = ("<html><head><style>.x{}</style></head><body>"
            "<nav>Home | Login | Sign up</nav>"
            "<header>Brand Slogan</header>"
            "<p>Real content paragraph about hexiel the coding agent.</p>"
            "<script>alert('tracker')</script>"
            "<footer>All rights reserved</footer></body></html>")
    text = html_to_text(html)
    assert "Real content paragraph" in text
    for noise in ("Home | Login", "Brand Slogan", "alert('tracker')", "All rights reserved"):
        assert noise not in text, noise


def test_compact_elides_stale_tools_without_summarizing(tmp_path):
    """Pre-step: when stamping out stale tool output is all that's needed,
    no provider call is made at all."""
    from hexiel.config import load_config
    from hexiel.tools import build_tools

    cfg = load_config()
    tools = build_tools(Toolchain())
    session = Session(cfg.models["glm-flash"], cfg.settings,
                      cwd=str(tmp_path), tools=tools, system_prompt="s")
    big = "r" * 20_000
    session.messages = [
        {"role": "user", "content": "oldest task"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "name": "grep", "arguments": {"pattern": "p"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": big},
        {"role": "user", "content": "middle task"},
        {"role": "assistant", "content": "did the middle thing"},
        {"role": "user", "content": "new task"},
    ]
    session.last_usage = {}
    events = []
    session.on_event = lambda *a, **k: events.append(a[0])
    session.compact(None)
    # no summarized compaction happened — elision alone sufficed
    assert events == ["compact_end"]
    assert session.messages[2]["content"] == "[prior tool output elided: ~20000 chars]"

def test_python_tool_batches_locally(tmp_path):
    from hexiel.tools import _run_python
    (tmp_path / "a.log").write_text("ok\nERROR one\nok\n")
    (tmp_path / "b.log").write_text("ERROR two\nERROR three\n")
    code = ("import glob\n"
            "n = sum(open(f).read().count('ERROR') for f in glob.glob('*.log'))\n"
            "print('errors:', n)\n")
    r = _run_python({"code": code}, ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    assert not r.is_error and r.text.strip() == "errors: 3"
    bad = _run_python({"code": "raise SystemExit('boom')"}, ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    assert bad.is_error and "boom" in bad.text


def test_skills_index_is_compact():
    from hexiel.skills import Skill, prompt_section, short_description
    long = "Use this skill when the user wants X, e.g. a report. " + "Lots more detail. " * 50
    assert short_description(long).startswith("Use this skill when the user wants X, e.g. a report")
    assert len(short_description(long)) <= 90
    sk = [Skill(name=f"s{i}", description=long, path=Path(f"/x/s{i}/SKILL.md"), body="") for i in range(15)]
    assert len(prompt_section(sk)) < 15 * 110 + 200     # was ~15 × 1000+ chars
