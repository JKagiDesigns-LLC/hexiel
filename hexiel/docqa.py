"""Document Q&A with citations (NotebookLM-style), done locally and cheaply.

docs_index  extract PDFs / Word / Markdown / text / HTML into passages (with page
            numbers) stored in a SQLite FTS5 index (.hexiel/docs.db); only changed
            files are re-read
docs_search best few passages for a question, tagged [file p.N] — the model
            cites them instead of reading whole documents

Ranking is BM25 (FTS5). If an Ollama endpoint with an embedding model is
reachable (e.g. nomic-embed-text), the top BM25 hits are re-ranked by meaning
too (hybrid via reciprocal-rank fusion). Everything runs on this machine."""

import json
import math
import re
import sqlite3
import zipfile
from pathlib import Path

from .tools import ToolDef, ToolResult, _resolve_path, html_to_text, text_result

DOC_EXTS = {".pdf", ".docx", ".md", ".markdown", ".txt", ".html", ".htm", ".rst"}
CHUNK, OVERLAP = 1200, 200
MAX_VECTOR_SCAN = 20_000        # brute-force semantic scan up to this many passages
SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".hexiel"}


def _db(ctx) -> sqlite3.Connection:
    path = Path(ctx.cwd) / ".hexiel" / "docs.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, mtime INTEGER, passages INTEGER);
        CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5(path UNINDEXED, page UNINDEXED, text,
                                                                tokenize='porter unicode61');
        CREATE TABLE IF NOT EXISTS vectors (rowid INTEGER PRIMARY KEY, vec TEXT);
    """)
    return con


# ---------------------------------------------------------------- extraction

def _pages(path: Path) -> list[tuple[str, str]]:
    """[(page label, text)] for one document."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("PDF support needs pypdf: pip install pypdf") from exc
        reader = PdfReader(str(path))
        return [(f"p.{i + 1}", page.extract_text() or "") for i, page in enumerate(reader.pages)]
    if suffix == ".docx":
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml").decode("utf8", "replace")
        paras = [re.sub(r"<[^>]+>", "", p) for p in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)]
        text = "\n".join(p for p in (re.sub(r"\s+", " ", x).strip() for x in paras) if p)
        return [("", text)]
    raw = path.read_text(errors="replace")
    if suffix in (".html", ".htm"):
        raw = html_to_text(raw)
    return [("", raw)]


def _chunks(text: str) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) <= CHUNK:
        return [text] if text else []
    out, start = [], 0
    while start < len(text):
        end = min(start + CHUNK, len(text))
        if end < len(text):                      # break on a sentence/paragraph if possible
            cut = max(text.rfind("\n", start + CHUNK // 2, end), text.rfind(". ", start + CHUNK // 2, end))
            end = cut + 1 if cut > 0 else end
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - OVERLAP, start + 1)
    return [c for c in out if c]


# ---------------------------------------------------------------- embeddings (optional)

def _embed_endpoint(ctx) -> tuple[str, str] | None:
    """An Ollama server with an embedding model, if any profile points at one."""
    import httpx
    from .config import load_config
    seen = set()
    try:
        models = load_config().models.values()
    except SystemExit:
        return None
    for mc in models:
        base = (mc.base_url or "").rstrip("/")
        if ":11434" not in base or base in seen:
            continue
        seen.add(base)
        native = base[:-3] if base.endswith("/v1") else base
        try:
            tags = httpx.get(f"{native}/api/tags", timeout=3).json().get("models", [])
        except (httpx.HTTPError, ValueError):
            continue
        for m in tags:
            if "embed" in m["name"]:
                return native, m["name"]
    return None


def _embed(endpoint, texts: list[str]) -> list[list[float]] | None:
    import httpx
    native, model = endpoint
    try:
        r = httpx.post(f"{native}/api/embed", json={"model": model, "input": texts}, timeout=120)
        return r.json().get("embeddings")
    except (httpx.HTTPError, ValueError):
        return None


def _cos(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


# ---------------------------------------------------------------- tools

def _index(args: dict, ctx) -> ToolResult:
    root = _resolve_path(args.get("path") or ".", ctx)
    files = [root] if root.is_file() else [
        p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in DOC_EXTS
        and not any(part in SKIP for part in p.relative_to(root).parts)]
    con = _db(ctx)
    known = dict(con.execute("SELECT path, mtime FROM files"))
    endpoint = _embed_endpoint(ctx) if args.get("semantic", True) else None
    added = skipped = 0
    errors = []
    for f in files[:2000]:
        key = str(f.relative_to(Path(ctx.cwd))) if f.is_relative_to(Path(ctx.cwd)) else str(f)
        mtime = f.stat().st_mtime_ns
        if known.get(key) == mtime:
            skipped += 1
            continue
        try:
            pages = _pages(f)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the index
            errors.append(f"{key}: {exc}")
            continue
        old = [r for (r,) in con.execute("SELECT rowid FROM passages WHERE path = ?", (key,))]
        con.executemany("DELETE FROM vectors WHERE rowid = ?", [(r,) for r in old])
        con.execute("DELETE FROM passages WHERE path = ?", (key,))
        rows = [(key, label, chunk) for label, text in pages for chunk in _chunks(text)]
        ids = []
        for row in rows:
            ids.append(con.execute("INSERT INTO passages (path, page, text) VALUES (?, ?, ?)", row).lastrowid)
        if endpoint and rows:
            for i in range(0, len(rows), 32):
                vecs = _embed(endpoint, [r[2] for r in rows[i:i + 32]])
                if not vecs:
                    endpoint = None             # embeddings unavailable: BM25 only
                    break
                con.executemany("INSERT OR REPLACE INTO vectors (rowid, vec) VALUES (?, ?)",
                                [(rid, json.dumps([round(x, 5) for x in v])) for rid, v in zip(ids[i:i + 32], vecs)])
        con.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?)", (key, mtime, len(rows)))
        con.commit()
        added += 1
    total = con.execute("SELECT count(*) FROM passages").fetchone()[0]
    nfiles = con.execute("SELECT count(*) FROM files").fetchone()[0]
    msg = (f"indexed {added} new/changed document(s), {skipped} unchanged — {nfiles} document(s), "
           f"{total} passage(s) in .hexiel/docs.db"
           + ("; semantic re-ranking on" if endpoint else "; keyword search (no embedding model found)"))
    if errors:
        msg += "\ncould not read: " + "; ".join(errors[:5])
    return text_result(msg)


STOPWORDS = set("""a an and are as at be been but by can could did do does doing for from had has have
how i if in into is it its just me my of on or our should so than that the their them then there these
they this those to us was we were what when where which while who whom why will with would you your
about after again all also any because before being between both during each few further here more most
other over own same some such through too under until very off out up down only nor not""".split())


def _fts_query(q: str) -> str:
    words = [w for w in re.findall(r"\w+", q.lower()) if len(w) > 2 and w not in STOPWORDS]
    return " OR ".join(f'"{w}"' for w in words[:24]) or '""'


def _search(args: dict, ctx) -> ToolResult:
    query = str(args.get("query", "")).strip()
    k = max(1, min(int(args.get("k", 6)), 12))
    if not query:
        return text_result("error: query is required", True)
    db = Path(ctx.cwd) / ".hexiel" / "docs.db"
    if not db.exists():
        return text_result("no document index yet — run docs_index on the folder first", True)
    con = _db(ctx)
    keyword = [r for (r,) in con.execute(
        "SELECT rowid FROM passages WHERE passages MATCH ? ORDER BY bm25(passages) LIMIT 50",
        (_fts_query(query),))]
    meaning: list[int] = []
    n_vec = con.execute("SELECT count(*) FROM vectors").fetchone()[0]
    if n_vec and n_vec <= MAX_VECTOR_SCAN:
        endpoint = _embed_endpoint(ctx)
        qv = _embed(endpoint, [query]) if endpoint else None
        if qv:       # semantic search over ALL passages: finds answers that share no words
            scored = [(rid, _cos(qv[0], json.loads(vec))) for rid, vec in con.execute("SELECT rowid, vec FROM vectors")]
            meaning = [rid for rid, _ in sorted(scored, key=lambda x: -x[1])[:50]]
    if not keyword and not meaning:
        return text_result("no matching passages — try other words, or check the index covers the documents")
    score: dict[int, float] = {}
    for ranking in (keyword, meaning):                   # reciprocal-rank fusion of both lists
        for rank, rid in enumerate(ranking):
            score[rid] = score.get(rid, 0) + 1 / (60 + rank)
    ranked = sorted(score, key=lambda r: -score[r])
    out = []
    for rid in ranked[:k]:
        row = con.execute("SELECT path, page, text FROM passages WHERE rowid = ?", (rid,)).fetchone()
        if row is None:
            continue
        path, page, text = row
        cite = f"[{path}{' ' + page if page else ''}]"
        out.append(f"{cite}\n{text.strip()[:1100]}")
    return text_result("\n\n".join(out) + "\n\n(cite passages by their [file p.N] tags)")


def doc_tools() -> list[ToolDef]:
    o = lambda **p: {"type": "object", "properties": p}  # noqa: E731
    return [
        ToolDef("docs_index", "Index a folder (or file) of documents — PDF, Word .docx, Markdown, text, "
                "HTML — for docs_search. Fast to re-run: only changed files are re-read.",
                o(path={"type": "string", "description": "folder or file, default the project"}),
                readonly=True, run=_index),
        ToolDef("docs_search", "Find the passages that answer a question across the indexed documents. "
                "Returns the best few passages tagged [file p.N] — answer from them and cite the tags "
                "instead of reading whole documents.",
                o(query={"type": "string"}, k={"type": "integer", "description": "passages, default 6"}),
                readonly=True, run=_search),
    ]
