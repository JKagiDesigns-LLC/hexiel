"""Deep research: parallel search + fetch, dedupe, per-question passage selection, citations."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import research as R
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext

PAGES = {
    "https://a.example/indexnow": "Menu Home About Login. IndexNow is a protocol that lets websites notify "
        "search engines instantly when content changes. It was launched by Bing and Yandex in 2021. "
        "Cookie settings Accept all.",
    "https://b.example/engines": "Search engines that support IndexNow include Bing, Yandex, Naver, Seznam "
        "and Yep. Submitted URLs are shared between all participating engines.",
    "https://c.example/google": "Google does not participate in IndexNow; it relies on sitemaps and crawling.",
}


def test_research_evidence_pack(monkeypatch, tmp_path):
    def fake_search(q, limit, ctx):
        urls = list(PAGES) + ["https://a.example/indexnow?utm=x"]        # duplicate with tracking params
        return [{"title": u.split("/")[-1], "url": u, "snippet": ""} for u in urls]
    monkeypatch.setattr(R, "search_results", fake_search)
    monkeypatch.setattr(R, "fetch_text", lambda url, ctx, t=15: PAGES[url.split("?")[0]])
    out = R._research({"question": "What is IndexNow and who supports it?",
                       "sub_questions": ["What is IndexNow", "Which search engines support IndexNow",
                                         "Does Google support IndexNow"]},
                      ToolContext(cwd=str(tmp_path), chain=Toolchain())).text
    assert "3 sources searched" in out                                   # duplicate URL dropped
    engines = out.split("## Which search engines support IndexNow")[1].split("##")[0]
    assert "Naver, Seznam" in engines
    google = out.split("## Does Google support IndexNow")[1].split("##")[0]
    assert "does not participate" in google
    assert "Cookie settings" not in out and "Menu Home" not in out      # boilerplate trimmed
    assert "## Sources" in out and "https://b.example/engines" in out
