"""Knowledge library: topic folders, incremental sync, deletion, hybrid search, topic scoping."""

import sys
from array import array
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import knowledge
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext


@pytest.fixture
def lib(tmp_path, monkeypatch):
    root = tmp_path / "knowledge"
    monkeypatch.setenv("HEXIEL_KNOWLEDGE", str(root))
    monkeypatch.setattr(knowledge, "_embedder", lambda auto_pull, say: None)   # keyword-only by default
    (root / "tomcat-10.1").mkdir(parents=True)
    (root / "java-21").mkdir()
    (root / "java-8").mkdir()
    (root / "tomcat-10.1" / "ssl.md").write_text(
        "# Connector\n\n## SSL\nSet `SSLEnabled=\"true\"` on the Connector element in server.xml.\n")
    (root / "java-21" / "records.md").write_text(
        "# Language\n\n## Records\nA record is a transparent carrier; use `record Point(int x, int y)`.\n")
    (root / "java-8" / "records.md").write_text(
        "# Language\n\n## Records\nRecords do not exist in this release; write a final class with getters.\n")
    return root


def test_sync_indexes_topics_and_search_cites(lib):
    s = knowledge.sync(say=lambda _m: None)
    assert s["added"] == 3 and s["errors"] == []
    assert {t for t, _, _ in knowledge.topics()} == {"tomcat-10.1", "java-21", "java-8"}
    text, err = knowledge.search("enable SSL connector server.xml")
    assert not err and "[tomcat-10.1/ssl.md]" in text and "Connector > SSL" in text


def test_topic_scoping_keeps_versions_apart(lib):
    knowledge.sync(say=lambda _m: None)
    text, _ = knowledge.search("records", topic=["java-21"])
    assert "transparent carrier" in text and "do not exist" not in text
    text, _ = knowledge.search("records", topic=["java"])           # prefix matches both versions
    assert "java-21" in text and "java-8" in text
    text, err = knowledge.search("records", topic=["python"])
    assert err and "available" in text


def test_incremental_sync_handles_change_and_delete(lib):
    knowledge.sync(say=lambda _m: None)
    again = knowledge.sync(say=lambda _m: None)
    assert again["unchanged"] == 3 and again["added"] == again["changed"] == again["removed"] == 0
    (lib / "tomcat-10.1" / "ssl.md").write_text("# SSL\nUse the new protocol attribute.\n")
    (lib / "java-8" / "records.md").unlink()
    s = knowledge.sync(say=lambda _m: None)
    assert s["changed"] == 1 and s["removed"] == 1
    assert "protocol attribute" in knowledge.search("protocol")[0]
    assert "do not exist" not in knowledge.search("records")[0]


def test_empty_library_and_hidden_files_ignored(lib):
    (lib / ".index").mkdir(exist_ok=True)
    (lib / ".index" / "junk.md").write_text("# nope")
    s = knowledge.sync(say=lambda _m: None)
    assert s["added"] == 3
    assert "nothing indexed" in knowledge.status() or "tomcat-10.1" in knowledge.status()


def test_empty_search_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_KNOWLEDGE", str(tmp_path / "none"))
    text, err = knowledge.search("anything")
    assert err and "hexiel knowledge sync" in text
    assert knowledge.prompt_section(str(tmp_path)) == ""


def test_semantic_ranking_and_model_change_rebuild(lib, monkeypatch):
    """Fake embedder: vector = [has 'ssl', has 'record'] so meaning, not words, picks the passage."""
    model = {"name": "fake-embed:1"}
    monkeypatch.setattr(knowledge, "_embedder", lambda auto_pull, say: ("http://x", model["name"]))

    def fake_embed(endpoint, texts, query=False):
        out = []
        for t in texts:
            t = t.lower()
            out.append(array("f", [1.0 if ("ssl" in t or "https" in t or "encrypt" in t) else 0.0,
                                   1.0 if "record" in t else 0.0, 0.1]))
        return out
    monkeypatch.setattr(knowledge, "_embed", fake_embed)
    s = knowledge.sync(say=lambda _m: None)
    assert s["embedded"] == 3
    text, _ = knowledge.search("encrypt traffic over https", topic=["tomcat"])   # shares no keywords with the doc
    assert "SSLEnabled" in text
    model["name"] = "other-embed:2"
    msgs = []
    s = knowledge.sync(say=msgs.append)
    assert s["embedded"] == 3 and any("re-embedding" in m for m in msgs)


def test_project_pin_and_tool(lib):
    knowledge.sync(say=lambda _m: None)
    proj = lib.parent / "proj"
    (proj / ".hexiel").mkdir(parents=True)
    (proj / ".hexiel" / "knowledge.toml").write_text('topics = ["java-21"]\n')
    ctx = ToolContext(cwd=str(proj), chain=Toolchain())
    tool = next(t for t in knowledge.knowledge_tools() if t.name == "knowledge_search")
    out = tool.run({"query": "records"}, ctx).text
    assert "transparent carrier" in out and "do not exist" not in out
    assert "java-21" in knowledge.prompt_section(str(proj)) and "pinned" in knowledge.prompt_section(str(proj))


def test_cli_status_and_sync(lib, capsys):
    assert knowledge.cli(["sync", "--no-embed"]) == 0
    assert knowledge.cli(["search", "records", "-t", "java-21"]) == 0
    out = capsys.readouterr().out
    assert "3 added" in out and "transparent carrier" in out
