"""Symbol index, find/where_is/callers/outline, read(symbol=/around=), and the freshness-stamped tool cache."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import symbols as sm
from hexiel.config import ModelConfig, Settings
from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, build_tools

PY = '''"""Config loading."""
import os


def load_config(path):
    """Read the settings file and return a Config."""
    return Config(parse(path))


class Config:
    """Holds settings."""

    def get(self, key):
        return self.data[key]

    def budget_limit(self):
        """The spend limit for this cycle."""
        return load_config("x")
'''
JS = '''// Render the sidebar.
export function renderSidebar(items) {
  for (const it of items) {
    draw(it);
  }
}

class Picker {
  open(name) {
    return name;
  }
}

const fetchModels = async (url) => {
  return url;
};
'''
GO = '''package main

// Serve starts the HTTP server.
func Serve(addr string) error {
	return nil
}

type Store struct {
	path string
}

func (s *Store) Load() error {
	return nil
}
'''


import pytest


@pytest.fixture(params=["tree-sitter", "regex"], autouse=True)
def engine(request, monkeypatch):
    """Every symbol test runs on both engines: tree-sitter (the 'code' extra) and the regex outlines."""
    if request.param == "tree-sitter" and not sm.treesitter_available():
        pytest.skip("tree-sitter extra not installed")
    monkeypatch.setattr(sm, "ENGINE", request.param)
    return request.param


def project(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "config.py").write_text(PY)
    (tmp_path / "web.js").write_text(JS)
    (tmp_path / "main.go").write_text(GO)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("function junk() {}\n")
    sm._INDEXES.clear()
    return sm.index_for(str(tmp_path))


def tools_for(tmp_path):
    ctx = ToolContext(cwd=str(tmp_path), chain=Toolchain())
    return {t.name: t for t in build_tools(Toolchain())}, ctx


def test_python_js_go_symbols_with_ranges(tmp_path):
    idx = project(tmp_path)
    names = {s.qualname: s for s in idx.all()}
    assert names["load_config"].line == 5 and names["load_config"].end == 7
    assert names["load_config"].doc.startswith("Read the settings file")
    assert names["Config.get"].kind == "method"
    assert {"renderSidebar", "Picker", "fetchModels", "Serve", "Store", "Load"} <= {s.name for s in idx.all()}
    assert names["renderSidebar"].end == 6 and names["renderSidebar"].doc == "Render the sidebar."
    assert names["Serve"].doc == "Serve starts the HTTP server."
    assert not any(s.path.startswith("node_modules") for s in idx.all())


def test_fuzzy_lookup_tolerates_typos_and_squashed_names(tmp_path):
    idx = project(tmp_path)
    for q in ("load_config", "loadconfig", "lod_confg", "LoadConfig"):
        assert sm.lookup(idx, q, 3)[0][1].name == "load_config", q
    assert sm.lookup(idx, "Config.get", 1)[0][1].qualname == "Config.get"


def test_find_ranks_docstrings_and_names(tmp_path):
    idx = project(tmp_path)
    hits = sm.find(idx, "spend limit")
    assert hits and "budget_limit" in hits[0][2]
    assert any("http server" in h[2].lower() for h in sm.find(idx, "start the http server"))


def test_callers_name_the_enclosing_function(tmp_path):
    idx = project(tmp_path)
    lines = sm.callers(idx, "load_config")
    assert any("Config.budget_limit" in ln for ln in lines)
    assert not any(":5" in ln.split(" ")[0] for ln in lines)          # the definition itself is not a caller


def test_tools_and_read_by_symbol_or_line(tmp_path):
    project(tmp_path)
    tools, ctx = tools_for(tmp_path)
    out = tools["where_is"].run({"name": "lod_confg"}, ctx).text
    assert "app/config.py:5-7" in out and "no exact match" in out
    out = tools["read"].run({"path": "app/config.py", "symbol": "Config.get"}, ctx).text
    assert "def get(self, key)" in out and "load_config" not in out
    out = tools["read"].run({"path": "app/config.py", "around": 14, "context": 1}, ctx).text
    assert "    13  " in out and "    15  " in out and "    16  " not in out and "def get" in out
    bad = tools["read"].run({"path": "app/config.py", "symbol": "nope"}, ctx)
    assert bad.is_error and "Config.budget_limit" in bad.text
    assert "budget_limit" in tools["outline"].run({"path": "app/config.py"}, ctx).text
    assert "Picker" in tools["symbols"].run({"kind": "class"}, ctx).text
    assert "Serve" in tools["find"].run({"query": "http server"}, ctx).text


def test_index_updates_only_changed_files_and_persists(tmp_path):
    idx = project(tmp_path)
    before = idx.files["main.go"]
    time.sleep(0.01)
    (tmp_path / "app" / "config.py").write_text(PY + "\n\ndef added():\n    pass\n")
    idx.refresh(min_interval=0)
    assert "added" in {s.name for s in idx.all()}
    assert idx.files["main.go"] is before                              # untouched file not re-parsed
    sm._INDEXES.clear()
    fresh = sm.Index(str(tmp_path))
    fresh._load()
    assert "added" in {s.name for s in fresh.all()}                    # warm from disk next session


def test_index_performance_budget(tmp_path):
    """~100k lines across 400 files: a cold index stays within a few seconds, lookups and find within the
    budget, and a no-change refresh is a stat walk (budgets are multiplied on a slow CI runner)."""
    from tests.conftest import SLOW
    (tmp_path / ".git").mkdir()
    body = "\n".join(f'def func_{i}(x):\n    """Does thing {i}."""\n    return x + {i}\n' for i in range(80))
    for n in range(400):
        (tmp_path / f"mod_{n}.py").write_text(body.replace("func_", f"m{n}_func_"))
    sm._INDEXES.clear()
    t = time.time()
    idx = sm.index_for(str(tmp_path))
    cold = time.time() - t
    assert len(idx.all()) == 32000 and not idx.partial
    assert cold < 8.0 * SLOW
    t = time.time()
    idx.refresh(min_interval=0)
    assert time.time() - t < 0.5 * SLOW
    t = time.time()
    assert sm.lookup(idx, "m399_func_79", 1)[0][1].name == "m399_func_79"
    assert time.time() - t < 1.5 * SLOW
    t = time.time()
    sm.find(idx, "does thing 42")
    assert time.time() - t < 2.0 * SLOW


# ---------------------------------------------------------------- the stamped cache and diff-aware re-reads

def session_in(tmp_path):
    tools = list(build_tools(Toolchain()))
    s = Session(ModelConfig(name="t", provider="openai", model="f", base_url="http://x"), Settings(True),
                cwd=str(tmp_path), tools=tools, system_prompt="s")
    s.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    s.on_event = lambda *a, **k: None
    return s


def run(s, n, name, **args):
    call = {"id": f"c{n}", "name": name, "arguments": args}
    s.messages.append({"role": "assistant", "content": "", "tool_calls": [call]})
    s._execute_tool(call)
    return s.messages[-1]["content"]


def test_cache_never_serves_a_stale_read_and_sends_a_diff_instead(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("\n".join(f"line {i}" for i in range(1, 201)) + "\n")
    s = session_in(tmp_path)
    first = run(s, 1, "read", path="a.py")
    assert "line 150" in first
    again = run(s, 2, "read", path="a.py")
    assert "unchanged since the identical earlier call" in again
    time.sleep(0.01)
    f.write_text(f.read_text().replace("line 150\n", "line 150 CHANGED\n"))
    third = run(s, 3, "read", path="a.py")
    assert "changed since your earlier read" in third and "+line 150 CHANGED" in third
    assert len(third) < len(first) / 4
    # once the earlier read is gone from the context, the whole file comes back
    for m in s.messages:
        if m.get("role") == "tool":
            m["content"] = "[cleared: read]"
    f.write_text(f.read_text().replace("line 10\n", "line 10 AGAIN\n"))
    fourth = run(s, 4, "read", path="a.py")
    assert "line 10 AGAIN" in fourth and "line 199" in fourth


def test_tree_search_cache_goes_stale_after_a_write(tmp_path):
    (tmp_path / "a.txt").write_text("needle one\n")
    s = session_in(tmp_path)
    s.settings.approve_mode = "auto"
    assert "needle one" in run(s, 1, "grep", pattern="needle")
    assert "cached" in run(s, 2, "grep", pattern="needle")
    s.tool_ctx.generation += 1                          # what any write/edit/bash call does
    (tmp_path / "a.txt").write_text("needle two\n")
    out = run(s, 3, "grep", pattern="needle")
    assert "needle two" in out and "cached" not in out


# ---------------------------------------------------------------- find by meaning (embeddings)

CONCEPTS = [("money", "spend", "budget", "cost", "dollars"), ("picture", "image", "screenshot", "photo"),
            ("server", "http", "serve", "listen")]


def fake_embed(ep, texts):
    """A toy embedding: one dimension per concept, so 'dollars' and 'budget' are close without sharing a word."""
    out = []
    for t in texts:
        low = t.lower()
        out.append([float(sum(low.count(w) for w in words)) + 0.01 for words in CONCEPTS])
    return out


def test_find_ranks_by_meaning_when_an_embedder_is_there(tmp_path, monkeypatch):
    monkeypatch.delenv("HEXIEL_EMBED")
    idx = project(tmp_path)
    monkeypatch.setattr(sm, "embedder", lambda: ("http://x", "toy-embed"))
    monkeypatch.setattr(sm, "_embed", fake_embed)
    sm.embed_missing(idx, ("http://x", "toy-embed"))
    hits = sm.find(idx, "how many dollars may I use")          # no word in common with "spend limit"
    assert hits and "budget_limit" in hits[0][2]
    plain = sm.find(idx, "how many dollars may I use", semantic=False)
    assert not plain or "budget_limit" not in plain[0][2]
    # vectors persist next to the index and are reused
    fresh = sm.Index(str(tmp_path))
    sm._load_vectors(fresh, "toy-embed")
    assert len(fresh.vectors) == len(idx.all())


def test_find_without_an_embedder_is_keyword_only(tmp_path, monkeypatch):
    idx = project(tmp_path)
    monkeypatch.setattr(sm, "embedder", lambda: None)
    called = []
    monkeypatch.setattr(sm, "_embed", lambda ep, t: called.append(t))
    assert sm.find(idx, "spend limit") and not called
