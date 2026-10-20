"""Knowledge library from the web: HTML->Markdown, scoped polite crawl, remembered sources, refresh."""

import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import knowledge, knowledge_web

PAGE = """<html><head><title>{title}</title></head><body>
<nav><a href="/docs/v2/">menu junk</a></nav>
<main><h1>{title}</h1><p>{body} This paragraph is padded so the page counts as real documentation content
for the crawler, which ignores near-empty pages entirely.</p>
<h2>Example</h2><pre>server.listen(8080)</pre>{links}</main><footer>footer junk</footer></body></html>"""


@pytest.fixture
def site(tmp_path):
    d = tmp_path / "site"
    (d / "docs" / "v2").mkdir(parents=True)
    (d / "other").mkdir()
    def page(title, body, *links):
        return PAGE.format(title=title, body=body, links="".join(f'<a href="{l}">x</a>' for l in links))
    (d / "docs" / "v2" / "index.html").write_text(page("Guide", "Start with the guide.", "intro.html", "/other/x.html", "http://elsewhere.invalid/z"))
    (d / "docs" / "v2" / "intro.html").write_text(page("Intro", "Gadgets use the flux capacitor setting.", "index.html"))
    (d / "other" / "x.html").write_text(page("Outside", "Not part of the docs scope."))
    (d / "robots.txt").write_text("User-agent: *\nDisallow: /docs/v2/secret\n")
    (d / "docs" / "v2" / "secret.html").write_text(page("Secret", "Disallowed by robots."))
    handler = partial(SimpleHTTPRequestHandler, directory=str(d))
    handler.log_message = lambda *a, **k: None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_to_markdown_keeps_structure_and_drops_chrome():
    title, md, links = knowledge_web.to_markdown(PAGE.format(title="T", body="b" * 400, links='<a href="/a">a</a>'))
    assert title == "T" and md.startswith("# T") and "## Example" in md and "```\nserver.listen(8080)\n```" in md
    assert "menu junk" not in md and "footer junk" not in md and "/a" in links


def test_crawl_is_scoped_polite_and_idempotent(site, tmp_path):
    root = tmp_path / "kb"
    st = knowledge_web.crawl(site + "/docs/v2/", "gizmo-2", root, delay=0, say=lambda _m: None)
    assert st["pages"] == 2 and st["written"] == 2          # not /other, not the external host, not robots-blocked
    files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*.md"))
    assert len(files) == 2 and all(f.startswith("gizmo-2/") for f in files)
    intro = next(root.rglob("intro.md")).read_text()
    assert intro.startswith("# Intro") and f"Source: {site}/docs/v2/intro.html" in intro
    again = knowledge_web.crawl(site + "/docs/v2/", "gizmo-2", root, delay=0, say=lambda _m: None)
    assert again["written"] == 0 and again["unchanged"] == 2    # nothing rewritten -> sync stays incremental


def test_add_sync_search_and_refresh(site, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HEXIEL_KNOWLEDGE", str(tmp_path / "kb"))
    monkeypatch.setattr(knowledge, "_embedder", lambda auto_pull, say: None)
    assert knowledge.cli(["add", site + "/docs/v2/", "-t", "gizmo-2", "--pages", "10"]) == 0
    assert knowledge.cli(["sources"]) == 0
    text, err = knowledge.search("flux capacitor", topic=["gizmo"])
    assert not err and "gizmo-2" in text and "flux capacitor" in text
    assert knowledge.cli(["sync", "--refresh"]) == 0
    assert "web sources: 2 pages (0 new/changed)" in capsys.readouterr().out


def test_agent_tools(site, tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_KNOWLEDGE", str(tmp_path / "kb"))
    monkeypatch.setattr(knowledge, "_embedder", lambda auto_pull, say: None)
    tools = {t.name: t for t in knowledge.knowledge_tools()}
    assert not tools["knowledge_add"].readonly and tools["knowledge_find_docs"].readonly
    out = tools["knowledge_add"].run({"url": site + "/docs/v2/", "topic": "Gizmo 2"}, None)
    assert not out.is_error and "gizmo-2" in out.text
    assert "flux capacitor" in tools["knowledge_search"].run({"query": "flux capacitor", "topic": "gizmo-2"},
                                                            type("C", (), {"cwd": str(tmp_path)})()).text


def test_nav_links_are_followed_but_not_saved_as_text():
    """Docs sites keep their table of contents in <nav>/<aside>: the crawler must follow it."""
    _, md, links = knowledge_web.to_markdown(
        '<nav><a href="/toc/page2.html">Page 2</a></nav><main><h1>T</h1><p>' + "x" * 400 + "</p></main>")
    assert "/toc/page2.html" in links and "Page 2" not in md
