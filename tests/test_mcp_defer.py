"""Many MCP tools: their schemas stay out of the prompt and a small `mcp_search` tool loads matches on demand."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import mcp_client
from hexiel.tools import ToolDef, text_result


def mk(server, name, desc):
    return ToolDef(f"mcp__{server}__{name}", f"[MCP {server}] {desc}",
                   {"type": "object", "properties": {"x": {"type": "string", "description": "y" * 400}}},
                   run=lambda a, c: text_result("ok"))


DEFS = [mk("github", "create_issue", "Create an issue in a repository"),
        mk("github", "list_pull_requests", "List open pull requests"),
        mk("db", "run_query", "Run a SQL query against the database")]


def test_defer_only_past_the_threshold():
    s = lambda mode, n: SimpleNamespace(mcp_defer=mode, mcp_defer_tokens=n)    # noqa: E731
    assert not mcp_client.should_defer(DEFS, s("auto", 10_000))
    assert mcp_client.should_defer(DEFS, s("auto", 50))
    assert mcp_client.should_defer(DEFS, s("on", 10_000))
    assert not mcp_client.should_defer(DEFS, s("off", 1))
    assert not mcp_client.should_defer([], s("on", 1))


def test_search_ranks_by_name_and_description():
    assert mcp_client.search_deferred(DEFS, "create issue")[0].name == "mcp__github__create_issue"
    assert mcp_client.search_deferred(DEFS, "sql database")[0].name == "mcp__db__run_query"
    assert mcp_client.search_deferred(DEFS, "zzz nothing") == []


def test_mcp_search_registers_the_match_once():
    session = SimpleNamespace(tools=[])
    mcp_client.install_search(session, DEFS)
    search = session.tools[0]
    assert search.name == "mcp_search" and "github (2 tools)" in search.description and "db (1 tools)" in search.description
    out = search.run({"query": "pull requests"}, None)
    assert "mcp__github__list_pull_requests" in out.text and not out.is_error
    assert [t.name for t in session.tools].count("mcp__github__list_pull_requests") == 1
    search.run({"query": "pull requests"}, None)                      # again: not registered twice
    assert [t.name for t in session.tools].count("mcp__github__list_pull_requests") == 1
    assert search.run({"query": "zzz"}, None).is_error
