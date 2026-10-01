"""Deep research, the cheap way: the model plans, Python does the legwork.

The model passes a question and 3–8 sub-questions. Python runs every search
and page fetch in parallel, drops duplicate sources, splits pages into
passages, and keeps only the few passages most relevant to each sub-question
(BM25 over the fetched pages). The model receives one compact evidence pack
with numbered sources and writes the cited report — two model calls instead
of a long search → fetch → read → search chain."""

import math
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from .docqa import STOPWORDS
from .tools import ToolDef, ToolResult, fetch_text, search_results, text_result

PASSAGE = 700


def _terms(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in STOPWORDS]


def _passages(text: str) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n|(?<=[.!?])\s{2,}", text) if len(p.strip()) > 40]
    out, buf = [], ""
    for p in paras:
        if len(buf) + len(p) < PASSAGE:
            buf = f"{buf} {p}".strip()
        else:
            if buf:
                out.append(buf)
            buf = p[:PASSAGE * 2]
    if buf:
        out.append(buf)
    return out or ([text.strip()[:PASSAGE * 2]] if len(text.strip()) > 20 else [])


def _bm25(query: list[str], docs: list[list[str]], k1=1.4, b=0.75) -> list[float]:
    n = len(docs) or 1
    avg = sum(len(d) for d in docs) / n or 1
    df = Counter(t for d in docs for t in set(d))
    scores = []
    for d in docs:
        tf = Counter(d)
        s = 0.0
        for t in query:
            if t in tf:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(d) / avg))
        scores.append(s)
    return scores


def _research(args: dict, ctx) -> ToolResult:
    question = str(args.get("question", "")).strip()
    subs = [str(q).strip() for q in (args.get("sub_questions") or []) if str(q).strip()][:8] or [question]
    per_query = max(1, min(int(args.get("results_per_query", 4)), 8))
    if not question:
        return text_result("error: question is required", True)

    with ThreadPoolExecutor(max_workers=8) as pool:                       # all searches at once
        found = list(pool.map(lambda q: _safe(lambda: search_results(q, per_query, ctx)), subs))
    sources, seen = [], set()
    for results in found:
        for r in results or []:
            key = urlparse(r["url"])._replace(query="", fragment="").geturl().rstrip("/")
            if r.get("url") and key not in seen:
                seen.add(key)
                sources.append(r)
    sources = sources[:int(args.get("max_sources", 16))]
    if not sources:
        return text_result("no search results — try different sub-questions", True)

    with ThreadPoolExecutor(max_workers=8) as pool:                       # all fetches at once
        texts = list(pool.map(lambda s: _safe(lambda: fetch_text(s["url"], ctx, 15)), sources))
    passages = []                                                        # (source index, text)
    for i, text in enumerate(texts):
        for p in _passages(text or sources[i].get("snippet", "")):
            passages.append((i, p))
    tokenized = [_terms(p) for _, p in passages]

    used: set[int] = set()
    lines = [f"# Evidence for: {question}", ""]
    for q in subs:
        scores = _bm25(_terms(q + " " + question), tokenized)
        best, per_source = [], Counter()
        for idx in sorted(range(len(passages)), key=lambda j: -scores[j]):
            src, text = passages[idx]
            if scores[idx] <= 0 or per_source[src] >= 1:     # one passage per source per question
                continue
            best.append((src, text))
            per_source[src] += 1
            if len(best) == 3:
                break
        lines.append(f"## {q}")
        if not best:
            lines.append("- (nothing relevant found)")
        for src, text in best:
            used.add(src)
            lines.append(f"- [{src + 1}] {_focus(text, set(_terms(q + ' ' + question)))}")
        lines.append("")
    lines.append("## Sources")
    for i in sorted(used):
        s = sources[i]
        lines.append(f"[{i + 1}] {s.get('title', '').strip()[:100]} — {s['url']}")
    failed = sum(1 for t in texts if not t)
    lines.append(f"\n({len(sources)} sources searched, {len(sources) - failed} read; full page text is "
                 f"cached under .hexiel/webcache/ if a detail needs checking. Cite claims as [n].)")
    return text_result("\n".join(lines))


def _focus(text: str, terms: set[str], budget: int = 520) -> str:
    """Keep the sentences that carry the query terms (in their original order),
    dropping navigation/boilerplate around them."""
    sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+", " ".join(text.split())) if len(x.strip()) > 25]
    if not sentences:
        return text[:budget]
    overlap = [len(terms & set(_terms(x))) for x in sentences]
    ranked = sorted(range(len(sentences)), key=lambda i: -overlap[i])
    if max(overlap) > 0:                    # boilerplate shares no words with the question
        ranked = [i for i in ranked if overlap[i] > 0]
    keep, used = set(), 0
    for i in ranked:
        if used + len(sentences[i]) > budget and keep:
            break
        keep.add(i)
        used += len(sentences[i])
    return " ".join(sentences[i] for i in sorted(keep))[:budget + 80]


def _safe(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - one dead link or blocked search must not sink the batch
        return None


def research_tools() -> list[ToolDef]:
    return [ToolDef(
        "research", "Deep web research in one step: give the main question and 3-8 focused "
        "sub-questions; hexiel searches and reads the web for all of them in parallel and returns "
        "an evidence pack — the most relevant passages per sub-question with numbered sources. "
        "Write the answer from it, citing [n]. Much cheaper than chaining web_search/web_fetch.",
        {"type": "object", "properties": {
            "question": {"type": "string"},
            "sub_questions": {"type": "array", "items": {"type": "string"}},
            "results_per_query": {"type": "integer", "description": "search results per sub-question (default 4)"},
            "max_sources": {"type": "integer", "description": "default 16"}},
         "required": ["question", "sub_questions"]},
        readonly=True, run=_research)]
