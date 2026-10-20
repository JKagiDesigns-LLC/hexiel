# The knowledge library (RAG)

hexiel can keep a standing library of reference documentation — the exact versions of
Java, Angular, Tomcat, Docker, nginx… that you work with — and answer from it with
citations instead of from the model's (possibly out-of-date) memory.

It is **free, open source and entirely local**. Nothing is uploaded anywhere: the docs, the
index and the search all live on your machine. (This is separate from *Hexiel Remote*, the
paid relay for controlling hexiel from a browser or phone.)

## Quick start

```bash
hexiel knowledge find "Java 21 documentation"            # free web search (DuckDuckGo / your SearXNG)
hexiel knowledge add https://docs.oracle.com/en/java/javase/21/language/ -t java-21
hexiel knowledge add https://tomcat.apache.org/tomcat-10.1-doc/ -t tomcat-10.1
hexiel                                                    # ask: "how do I enable HTTPS in Tomcat?"
```

`add` downloads the site and indexes it; you do not need to run `sync` afterwards.

## The folder

```
hexiel knowledge path      # → ~/.local/share/hexiel/knowledge   (override: HEXIEL_KNOWLEDGE=/some/dir)

knowledge/
  java-21/            ← one folder per topic; the name is the "version tag" you search by
  tomcat-10.1/
  docker-compose/
  sources.toml        ← web sources remembered by `add`
  .index/             ← the search index (safe to delete; rebuilt by `sync --rebuild`)
```

Add your own material by copying files into a topic folder: Markdown, plain text, HTML,
reStructuredText/AsciiDoc, **PDF** and **Word (.docx)**. Subfolders are fine. Files in the
top level of `knowledge/` go to the topic `general`.

## Commands

| Command | What it does |
|---|---|
| `hexiel knowledge sync` | Index what is **new, changed or deleted** since last time. Fast to re-run; only touched files are re-read and re-embedded. |
| `hexiel knowledge sync --refresh` | First re-download every web source in `sources.toml`, then sync. |
| `hexiel knowledge sync --rebuild` | Throw the index away and rebuild it (e.g. after changing embedding model). |
| `hexiel knowledge add <url> -t <topic> [--pages N] [--depth N]` | Crawl a docs site into the topic folder and index it. Defaults: 200 pages, depth 3. `--depth 0` saves just that page. |
| `hexiel knowledge find <query>` | Web-search for the official docs of a technology and version. |
| `hexiel knowledge sources` | List remembered web sources. |
| `hexiel knowledge status` | Topics, files, passages, whether semantic search is on, index size, last sync. |
| `hexiel knowledge search <query> [-t topic]` | Try retrieval without a model — handy for checking what a doc set answers. |
| `/knowledge` · `/knowledge sync` | The same status / sync, inside the TUI and the browser UI. |

(`hexiel rag …` is an alias for `hexiel knowledge …`.)

## How the model uses it

- When the library has content, hexiel's system prompt lists your topics (one short block —
  nothing at all if the library is empty) and the `knowledge_search` tool is loaded.
- For version-specific questions the model searches **first** and cites the `[file]` it
  used. `topic` narrows the search; `java` matches `java-21` and `java-17`, `java-21`
  matches only that one.
- If the library lacks something, the model can `knowledge_find_docs` (web search) and offer
  `knowledge_add` to download the docs. Downloading asks for your approval like any other
  tool that changes things.
- Retrieved passages are treated as reference material, never as instructions.

### Pin a project to versions

Different projects use different versions. In the project, create `.hexiel/knowledge.toml`:

```toml
topics = ["java-17", "tomcat-10.1", "angular-18"]
```

Those topics are searched by default in that project (and named in the prompt).

## How search works

1. **Chunking** — documents are split into ~1,200-character passages; Markdown is split by
   heading and each passage remembers its trail (`Connector > SSL`). Web pages are converted
   to Markdown first (headings and code blocks kept, navigation stripped) and start with a
   `Source: <url>` line.
2. **Keyword search** — SQLite FTS5 with BM25 ranking. Exact terms (`server.xml`,
   `SSLEnabled`, `--network host`) are matched literally.
3. **Semantic search** — each passage is embedded with an Ollama embedding model
   (default `nomic-embed-text`; pulled automatically the first time you sync — about 270 MB).
   Questions are matched by meaning, so the words need not overlap.
4. **Fusion** — both rankings are merged with reciprocal-rank fusion and the best few
   passages are returned.

Without Ollama, step 3 is skipped and keyword search still works; run `sync` again once
Ollama is up and the missing embeddings are filled in. If you change the embedding model
(`HEXIEL_EMBED_MODEL`), hexiel re-embeds everything on the next sync. `pip install numpy`
makes semantic search over very large libraries (100k+ passages) much faster; it is optional.

## The crawler is polite

Same host only, and only under the start URL's folder; honours `robots.txt`; identifies
itself; waits between requests; skips images, archives and non-HTML; never rewrites a file
whose text did not change (so syncs stay incremental). Only add documentation you are
allowed to keep a copy of — most vendor docs are fine for personal use, but check the
site's terms.

## Troubleshooting

- *`add` saved only a few pages* — the crawl stays under the start URL's folder. Docs often
  live beside the landing page, not under it (angular.dev keeps its guides at `/essentials/…`
  and `/guide/…`, so start from `https://angular.dev/overview`, not `…/guide/`). Pick a start URL
  that is a parent of the pages you want, and raise `--pages` for big manuals (the
  default is 200; the Java SE *API* reference and similar are thousands of pages — fetch
  only the guides you need).
- *Syncing is slow the first time* — embedding runs on your Ollama machine. On a modest
  mini-PC expect roughly 1–2 passages per second; a 40-page site takes a few minutes.
  Later syncs only embed what changed.
- *"no Ollama embedding model available — keyword search only"* — start Ollama
  (`ollama serve`) and run `hexiel knowledge sync`.
- *`find` returns nothing* — DuckDuckGo sometimes throttles bursts; hexiel retries, or wait a
  few seconds. You can point `search_backend = "searxng"` in `config.toml` at your own instance.
- *A docs site is empty after `add`* — single-page apps that render in the browser
  have no content to download. Try the site's static/"print" docs, a GitHub mirror of its
  Markdown, or download the docs archive and copy it into the topic folder.
- *Searches return the wrong version* — keep one topic folder per version and pin the
  project (`.hexiel/knowledge.toml`) or pass `topic`.
