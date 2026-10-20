"""Fill the knowledge library from the web: find official docs, crawl a docs site.

`hexiel knowledge find "tomcat 10.1 documentation"` searches with hexiel's normal free
backend (DuckDuckGo, or your SearXNG). `hexiel knowledge add <url> -t tomcat-10.1` crawls
that docs site — same host, under the start URL's folder, robots.txt respected, rate
limited — and saves each page as Markdown (headings and code kept) in the topic folder,
with its source URL. Sources are remembered in `sources.toml`, so
`hexiel knowledge sync --refresh` re-crawls them all. Files whose text did not change
are left alone, so the index stays incremental.
"""

import html
import re
import time
import tomllib
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .tooling import Toolchain
from .tools import ToolContext, search_results

UA = "hexiel-knowledge/1.0 (+https://hexiel.tech; polite docs crawler)"
MAX_BYTES = 2_000_000
DROP = {"script", "style", "noscript", "template", "nav", "header", "footer", "aside", "form", "svg",
        "button", "select", "iframe"}
BLOCK = {"p", "div", "section", "article", "main", "ul", "ol", "table", "tr", "dl", "dt", "dd", "blockquote", "figure"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".pdf", ".zip", ".gz", ".tgz", ".jar", ".war",
            ".css", ".js", ".json", ".xml", ".ico", ".woff", ".woff2", ".mp4", ".exe", ".dmg"}


# ---------------------------------------------------------------- HTML -> Markdown

class _Md(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title, self._in_title = "", False
        self.out: list[str] = []          # whole body
        self.main: list[str] = []         # inside <main>/<article>, preferred when substantial
        self.links: list[str] = []
        self._skip = self._main = self._pre = 0
        self._href: list[str] = []

    def _w(self, s: str) -> None:
        if not self._skip:
            self.out.append(s)
            if self._main:
                self.main.append(s)

    def handle_starttag(self, tag, attrs) -> None:
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])         # sidebar/nav links count for crawling, though not as text
        if tag in DROP:
            self._skip += 1
        if self._skip:
            return
        if tag in ("main", "article"):
            self._main += 1
        if re.fullmatch(r"h[1-6]", tag):
            self._w("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "pre":
            self._pre += 1
            self._w("\n\n```\n")
        elif tag == "code" and not self._pre:
            self._w("`")
        elif tag == "li":
            self._w("\n- ")
        elif tag in ("br", "tr"):
            self._w("\n")
        elif tag in ("td", "th"):
            self._w(" | ")
        elif tag in BLOCK:
            self._w("\n\n")

    def handle_endtag(self, tag) -> None:
        if tag == "title":
            self._in_title = False
        if tag in DROP:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in ("main", "article"):
            self._main = max(0, self._main - 1)
        if re.fullmatch(r"h[1-6]", tag):
            self._w("\n\n")
        elif tag == "pre":
            self._pre = max(0, self._pre - 1)
            self._w("\n```\n\n")
        elif tag == "code" and not self._pre:
            self._w("`")
        elif tag in BLOCK:
            self._w("\n\n")

    def handle_data(self, data) -> None:
        if self._in_title:
            self.title += data
        elif self._pre:
            self._w(data)
        elif data.strip():
            self._w(re.sub(r"\s+", " ", data))


def to_markdown(html: str) -> tuple[str, str, list[str]]:
    """(title, markdown body, hrefs)."""
    p = _Md()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 - malformed pages must not stop a crawl
        pass
    body = "".join(p.main) if len("".join(p.main).strip()) > 300 else "".join(p.out)
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return re.sub(r"\s+", " ", p.title).strip(), body, p.links


# ---------------------------------------------------------------- crawl

def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", text.lower()).strip("-") or "docs"


def _scope(start: str) -> tuple[str, str]:
    u = urlparse(start)
    path = u.path if u.path.endswith("/") else u.path.rsplit("/", 1)[0] + "/"
    return u.netloc, path or "/"


def _canon(url: str) -> str:
    """Fragment-free URL with /index.html folded into its folder, so one page is fetched once."""
    return re.sub(r"/index\.(html?|php)$", "/", urldefrag(url)[0])


def _file_for(root: Path, topic: str, url: str) -> Path:
    u = urlparse(url)
    path = u.path
    if path.endswith("/") or not path:
        path += "index"
    path = re.sub(r"\.(html?|php|aspx?)$", "", path)
    parts = [slug(p) if p not in ("", ".", "..") else "x" for p in path.split("/") if p]
    return root / topic / slug(u.netloc) / Path(*parts).with_suffix(".md")


def crawl(url: str, topic: str, root: Path, max_pages: int = 200, depth: int = 3,
          delay: float = 0.3, say=print) -> dict:
    """Save a docs site under root/topic/. Returns counts."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    host, prefix = _scope(url)
    robots = RobotFileParser()
    try:
        r = httpx.get(f"{urlparse(url).scheme}://{host}/robots.txt", headers={"user-agent": UA}, timeout=10)
        robots.parse(r.text.splitlines() if r.status_code == 200 else [])
    except httpx.HTTPError:
        robots.parse([])
    stats = {"pages": 0, "written": 0, "unchanged": 0, "skipped": 0, "errors": []}
    queue, seen = [(_canon(url), 0)], {_canon(url)}
    with httpx.Client(timeout=30, follow_redirects=True, headers={"user-agent": UA}) as client:
        while queue and stats["pages"] < max_pages:
            page, d = queue.pop(0)
            if not robots.can_fetch(UA, page):
                stats["skipped"] += 1
                continue
            try:
                resp = client.get(page)
            except httpx.HTTPError as exc:
                stats["errors"].append(f"{page}: {exc}")
                continue
            ctype = resp.headers.get("content-type", "")
            if resp.status_code != 200 or "html" not in ctype or len(resp.content) > MAX_BYTES:
                stats["skipped"] += 1
                continue
            title, body, links = to_markdown(resp.text)
            if len(body) < 80:
                stats["skipped"] += 1
                continue
            seen.add(_canon(str(resp.url)))
            stats["pages"] += 1
            text = (body if body.lstrip().startswith("# ") else f"# {title or page}\n\n{body}")
            head, _, rest = text.partition("\n")
            text = f"{head}\n\nSource: {resp.url}\n{rest}\n"
            out = _file_for(root, topic, str(resp.url))
            if out.exists() and out.read_text() == text:
                stats["unchanged"] += 1
            else:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(text)
                stats["written"] += 1
            if stats["pages"] % 20 == 0:
                say(f"  fetched {stats['pages']} pages…")
            if d < depth:
                for href in links:
                    nxt = _canon(urljoin(str(resp.url), href))
                    u = urlparse(nxt)
                    if (u.scheme in ("http", "https") and u.netloc == host and u.path.startswith(prefix)
                            and Path(u.path).suffix.lower() not in SKIP_EXT and not u.query and nxt not in seen):
                        seen.add(nxt)
                        queue.append((nxt, d + 1))
            time.sleep(delay)
    return stats


# ---------------------------------------------------------------- remembered sources

def sources_file(root: Path) -> Path:
    return root / "sources.toml"


def load_sources(root: Path) -> list[dict]:
    try:
        return tomllib.loads(sources_file(root).read_text()).get("source", [])
    except (OSError, tomllib.TOMLDecodeError):
        return []


def save_source(root: Path, url: str, topic: str, max_pages: int, depth: int) -> None:
    items = [s for s in load_sources(root) if not (s.get("url") == url and s.get("topic") == topic)]
    items.append({"url": url, "topic": topic, "max_pages": max_pages, "depth": depth})
    root.mkdir(parents=True, exist_ok=True)
    sources_file(root).write_text("# Web sources hexiel keeps fresh: `hexiel knowledge sync --refresh`\n" + "".join(
        f'\n[[source]]\nurl = "{s["url"]}"\ntopic = "{s["topic"]}"\n'
        f'max_pages = {int(s.get("max_pages", 200))}\ndepth = {int(s.get("depth", 3))}\n' for s in items))


def refresh_sources(root: Path, say=print) -> dict:
    total = {"pages": 0, "written": 0, "unchanged": 0, "skipped": 0, "errors": []}
    for s in load_sources(root):
        say(f"fetching {s['url']} → {s['topic']}/")
        st = crawl(s["url"], s["topic"], root, int(s.get("max_pages", 200)), int(s.get("depth", 3)), say=say)
        for k in ("pages", "written", "unchanged", "skipped"):
            total[k] += st[k]
        total["errors"] += st["errors"]
    return total


# ---------------------------------------------------------------- finding docs

def find_docs(query: str, limit: int = 8) -> list[dict]:
    """[{title, url, snippet}] from the configured free search backend."""
    from .config import load_config
    try:
        settings = load_config().settings
        backend, searx = settings.search_backend, settings.searxng_url
    except SystemExit:
        backend, searx = "duckduckgo", ""
    ctx = ToolContext(cwd=".", chain=Toolchain(), search_backend=backend, searxng_url=searx)
    for wait in (0, 3, 8):                  # DuckDuckGo throttles bursts by returning an empty page
        time.sleep(wait)
        found = search_results(query, limit, ctx)
        if found:
            break
    return [{**r, "title": html.unescape(r["title"])} for r in found]
