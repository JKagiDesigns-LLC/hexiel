# hexiel roadmap — in flight (started 2026-11-06)

Feature program from competitor research + performance/token research. Each feature ships in its
own pip release (tag vX → CI publishes), gets its GitLab ticket updated with a screenshot/copy as it
lands, and is documented on hexiel.tech the same day so the site never goes stale.

## Release 0.8.5 — performance + token economy (nothing user-visible broken; everything faster/cheaper)
- [x] Prefix-cache stability: git state + today's date moved out of the cached system prompt
      (env facts go to a separate fresh line in the payload tail; stable-prefix layers: tools →
      system → env → history).  Local Ollama PLLM/prefix cache also benefits.
- [x] keep_alive for Ollama (models stay resident; no more 30–120s reload after idle; configurable
      `[models.NAME] keep_alive = "-1"` with a session default in settings).
- [x] num_ctx pinned once per model (context never silently changes; cache not thrown away).
- [x] Pre-warm at startup: fire-and-receive an empty warm request so the first real message is fast.
- [x] prompt_cache_key (OpenAI) = session id; cache hit-rate surfaced in /usage (local: from
      prompt_eval_duration; OpenRouter: cache_discount) and on the sidebar/status.
- [x] /context — manual context meter: what's in the window (system/tools/history/tool results),
      with selective clear of old tool results / old turns.

## Release 0.8.6 — everyday UX (terminal + config discovery)
- [x] Terminal autocomplete: paths (@, ~, quoted, first-qualifier), /commands, /model names,
      Ctrl+R fuzzy reverse-history. (#25)
- [x] Cross-harness instruction discovery: .cursor/rules/*, .codex/(AGENTS/rules), GEMINI.md,
      .clinerules, COPILOT.md/.agent.md etc. read alongside CLAUDE.md/AGENTS.md (last wins, layered). (#26)
- [x] Session fork: /fork — branch the conversation from any point into a new session file. (#27)
- [x] Session share: /share — self-contained single-file HTML transcript (no server, no secrets)
      placed next to the session; open or attach anywhere. (#28)
- [x] Watch mode: /watch — hexiel stays alive and reacts to AI:/AI? comments saved in any editor. (#29)

## Release 0.8.7 — isolation + accuracy
- [x] Worktree isolation: /wt (list/create/open), sub+headless runs can run in their own worktree;
      parallel agent runs safe on one repo. Env files copied via post_setup hook (Devin-style). (#36)
- [x] LSP diagnostics tool: pyright/pylsp-based diagnos for python, tsc for TS/JS, go vet, cargo-
      check; surfaced as a read-only `diagnostics` tool + run after edits when available (cheap, local). (#38)
- [x] Formatter integration: black/ruff/prettier/gofmt detected; edits auto-format the touched
      region/format-on-write with formatter="off" escape hatch; instant-checks upgraded. (#39)
- [x] Sandboxing (opt-in safe profile): macOS sandbox-exec profile, Linux bubblewrap/landlock with
      sudo-config note plus container fallback (docker/podman); approval gates bypass only inside. (#40)
- [x] CI runner mode: `hexiel --ci` reads issue/commit context from env (GitLab first), runs
      headless with read-only default, comments results back via API token if provided. (#41)

## Release 0.8.10 — budget truth + knowledge that shows up by itself
- [x] SPEND bar follows the provider: Ollama's /api/balance gives the plan's own billing period, what is
      left of the allowance and the credits bought on top → budget = spent + left (a top-up grows the bar
      instead of overflowing it), "$ left · renews <date>", today's bill, this week's pace, "runs out ~<date>";
      a top-up is announced once; the same block in the web UI sidebar; /usage prints it all.
- [x] Knowledge library used automatically: topics detected from the project tree (manifests, dependency
      names, file types; version hints pick java-21 over java-8), searched by default, and passages that
      match a message are attached to that turn (keyword search, no model call, hard 150 ms budget, ≤1500
      chars, cached read-only connection). `knowledge_auto = "off"` turns it off; /knowledge shows the cost.

## Release 0.9.0 — doctor, :checkhealth style
- [x] doctor.py around a Check record (group, level OK/WARNING/ERROR/INFO, label, detail, advice lines,
      optional fix). Grouped output: Runtime · Models · Tools · Integrations · Project; counts at the end.
- [x] Reclassify the fresh-install false failures (memory dir, skills, test runner, GitLab token, "no
      config yet") to INFO/WARNING; "provider endpoint" is an ERROR when the default model is not in the
      server's inventory; the default model is proven with a one-token request (401 → "ollama signin").
- [x] New checks: Ollama Cloud sign-in state; each key env named by a profile (masked); PATH shadowing;
      terminal colours; pwsh on Windows; disk space; helper tools (pyright, ruff, prettier, tesseract…).
- [x] `--fix` applies the safe generic fixes (dirs, optional extras, pull the chosen local model);
      `--json` for CI and the web UI (/api/doctor). `/doctor` in the TUI and the web chat.
- [x] Tests with a fake home, fake endpoints and a fake probe; fresh install shows no ERROR.

## Release 0.9.1 — installer with discovery and guided setup
- [x] install.sh (macOS/Linux) + install.ps1 (Windows) on hexiel.tech: uv first (a pinned release, .sha256
      verified; brings Python), pipx or venv on request; never root; PATH for new terminals; last step runs
      `hexiel --setup` (from /dev/tty when piped). Smoke-tested in a clean Ubuntu container as a non-root user.
- [x] hexiel/discover.py — read-only scan, keys masked, nothing written before the review step:
      Claude Code (~/.claude, ~/.claude.json, skills, .mcp.json) · Codex (~/.codex auth.json/config.toml/
      AGENTS.md) · Gemini CLI (~/.gemini) · OpenCode (~/.local/share/opencode/auth.json, ~/.config/opencode)
      · Goose (~/.config/goose, %APPDATA%\Block\goose) · Aider/Continue · Cursor (mcp.json, rules) · Ollama
      (~/.ollama, localhost:11434, cloud sign-in) · shell rc files + ~/.env + Windows user env (registry) ·
      keychains via `keyring` (only entries other agents stored). Windows paths under %USERPROFILE%.
- [x] Rules: API keys only — subscription OAuth tokens (claude.ai, ChatGPT, Google logins) are detected,
      named, and refused with the API-key / Ollama alternative; keys go to ~/.config/hexiel/auth.toml
      (0600), never config.toml, and are exported at startup (the shell wins); every finding carries
      provenance; review y/n per row; unknown variables (a DB password in .env) are never taken.
- [x] Defaults after import: default profile = first that answers now (loaded local Ollama model → pulled
      model (profile added) → Ollama Cloud if signed in → cheapest profile with a working key), proven with
      one request. What was not found → one line per provider (sign-up link + exact command);
      `hexiel auth set NAME|VARIABLE`, `hexiel auth list`, `hexiel auth remove`.
- [x] Re-runnable: `hexiel --setup` / `hexiel setup` (auto on the very first terminal start), `/import` in
      the TUI and the web chat. (The web Setup page is in 0.9.2.)
- [x] Tests: fake-home fixtures (linux/mac/windows layouts), every source, "OAuth never written", "no leaves
      config untouched", auth CLI and startup export.
- [x] hexiel.tech install section redesigned around the two one-liners + "just works out of the box".

## Release 0.9.2 — trust: CI, plain-language errors, web parity
- [x] GitLab CI test stage on push + install smoke matrix (clean Ubuntu/macOS/Windows: install.sh →
      `hexiel doctor --json` → one `-p` prompt). Fix the order-dependent test_escalate failure.
      (conftest clears model cooldowns between tests; the pipeline runs once the branch is pushed.)
- [x] Errors in one sentence with the fix: dead server (retry capped and visible), missing sign-in (ADVICE
      line, not raw JSON), crashes ("saved as an incident, run /heal", not a traceback).
- [x] Web UI: welcome card with example prompts, Setup page (keys, model, budget), Health page.
- [x] Budget hard stop: `stop_at` per account refuses cloud requests past the budget → local fallback.
- [x] Website: lead with "ask questions about your spreadsheet or PDF" next to the coding demo (DuckDB,
      openpyxl, pypdf already in the box); 60-second bug-fix demo.

## Release 0.9.3 — Python does the looking: more visibility, fewer tokens (local models first)
- [x] Symbol index: tree-sitter over the project at startup (refreshed on change, persisted in .hexiel/);
      tools `symbols`/`outline`/`where_is`/`callers`; `read(path, symbol=…)` and `read(path, around=line)`.
- [x] One `find` tool merging rapidfuzz (typo-tolerant names), BM25 over docstrings/comments, and the
      embedding model when present — one call instead of a grep chain.
- [x] Vision for non-vision models: `[tool_models] vision = <small ollama vision model>` describes any
      image a non-vision chat model receives (structured card: text with positions, widgets, errors
      verbatim); no-model path = Pillow + tesseract layout + pyzbar; PDF pages rendered only when the text
      layer is empty; render-and-look for HTML/Markdown via a headless browser.
- [x] Capability inventory: PATH walk cached by PATH hash (formatters, linters, LSPs, tesseract, ImageMagick,
      ffmpeg, pandoc, jq, rg, gh/glab, cloud CLIs, databases, browsers, importable Python packages, OS/GPU/RAM);
      only the relevant slice in the prompt; `have(name)` + the doctor's install line when a tool is
      missing; tool definitions for absent binaries are not sent at all.
- [x] Structured command output (git status, test results, pip list, ls as data) and a no-model log
      condenser (dedupe, keep errors + tail, "N similar lines removed").
- [x] Auto-context on the first message (project type, entry points, how to test, recent commits, open
      todos) built locally; diff-aware reads after edits; tool-result cache keyed by args + file mtimes.
- [x] Deterministic helper tools: date math, units, regex test, JSON path, CSV stats, hash/base64, semver.
- [x] Performance budget for every item above: measured in tests (ms per call on a 100k-line repo and a
      20k-passage library); anything over budget degrades to "nothing attached", never to a slow turn.
- As built (differences from the plan above): the index is pure Python (`ast` + regex outlines with brace/indent
  scoping) rather than tree-sitter — no new dependency, 0.25 s cold on this repo, 32k definitions in the perf test;
  `find` uses its own fuzzy scorer and BM25, no rapidfuzz and no embeddings yet; `read(path, symbol=…)` and
  `read(path, around=…)` are on the existing `read`. Vision: describe-once for known text-only families (unknown
  models keep their images), `describe_image` tool, tesseract fallback; NOT yet: Pillow layout card, pyzbar, PDF page
  rendering when the text layer is empty, render-and-look for HTML. Inventory: catalogued programs only (no version
  calls), no LSP/database probing beyond PATH; definitions left out for powershell and container when absent.
  Performance budgets are asserted for the symbol index, find/lookup, the log condenser and the brief (0.6 s budget).
- [x] Follow-ups (0.9.4): tree-sitter as the `code` extra (exact ranges for 30+ languages; the regex outlines stay
      the no-dependency path, `HEXIEL_SYMBOLS=regex` forces them); `find` blends in embedding similarity when an
      embedding model answers (vectors cached next to the index, filled in the background, `HEXIEL_EMBED=off`);
      vision: Pillow image card (size, colours, where the content is, OCR text with positions, QR/barcodes via
      pyzbar or zbarimg), PDF pages without a text layer rendered and read, `render` tool (headless browser
      screenshot of HTML/Markdown/URLs, looked at or described).

## Deferred → built in 0.9.4
- [x] ACP server (serve hexiel into Zed/JetBrains): `hexiel --acp [-m PROFILE]`; new/load/prompt/cancel/set_mode,
      editor MCP servers, permission requests, tool cards with diffs, the todo list as the plan.
- [x] Agent teams (peer messaging layer on top of the delegate pattern): `team` tool, 2-6 named members in
      parallel, `team_msg` send/broadcast/wait delivered mid-turn, /team board.
- [x] Skills/plugin marketplace plumbing: `/marketplace` + `hexiel marketplace`; sources (URL, folder, git),
      hexiel and Claude Code marketplace.json indexes, sha256 pins, update/remove by install record.

## Release 0.9.5 — local models that misuse the tool channel
- [x] A tool call written into the reply text (gpt-oss's `{"cmd": [...]}` shell format, `{"name", "arguments"}`
      wrappers, the OpenAI wire format, or bare keys that fit exactly one tool) is turned back into a real call,
      so the turn keeps working. Only when the JSON comes last and sits outside a ``` block: examples in an
      answer are shown, never run.
- [x] "continue" / "go on" / "next" are never answered tool-less by the router's chat model: the main model
      takes them, with its tools.

## Release 0.9.6 — don't hammer a cloud account that can't answer; CI green (the CI fixes finished in 0.9.7)
- [x] A cloud account found unusable (rejected key, no credits, monthly limit, budget stop) is skipped for
      `cloud_cooldown_minutes` (default 60) by every chain — main, router, overflow, planner, vision, tool models —
      and across processes; checked once when a session starts; the session starts on the next usable model
      with a notice and returns by itself when the cooldown ends. `/accounts` shows it, `/accounts recheck` retries. A 401/402/403 now fails
      over to the next model instead of ending the turn.
- [x] qwen3.5 / qwen3.6 see images (llama-server with an mmproj): no describe detour for gmktec.
- [x] CI tests pass in the root container runner (sudo, clipboard, ruff, timing assumptions).

## Next — Python does the work: files, media and documents without model walls (researched 2026-11-12)
Principle: every tool is deterministic Python that returns a short text summary plus the output path, so ANY
model (text-only, small, local) can create and change images, video, documents and data. The model only sees
pixels when it can (and wants to). One tool per domain with an `op` argument (few tool definitions, loaded as
a group by load_tools); heavy libraries are optional extras (hexiel[media], [office], [pdf], [audio], [db])
offered by `have` and installed with approval into hexiel's own environment; every destructive op previews
first and writes an undo record. Tests use generated fixtures, no network.

### 0.9.7 — images: view, create, edit anything (ticket 878919f4975a)
- [x] `image` tool on Pillow (already a dependency): inspect (size, mode, EXIF, DPI, alpha, palette, histogram,
      the image card), view (terminal thumbnail; text description for blind models via the card + OCR)
- [x] edit: crop (box, auto-trim, aspect), resize/fit/pad/thumbnail, rotate/flip, brightness/contrast/
      saturation/gamma, blur/sharpen/filters, grayscale/sepia/invert, replace a colour, transparency,
      rounded corners, border/shadow, watermark (text or logo), text and shapes on top, redact a region
- [x] create: canvas with shapes/text/gradients, placeholders, banners, social cards, collage/grid/contact
      sheet, side-by-side, GIF from frames, QR codes (segno), favicons + app-store icon sets, sprite sheets
- [x] compare: pixel diff of two screenshots with the changed regions as boxes (visual regression)
- [x] convert/compress: PNG/JPEG/WebP/AVIF/ICO/TIFF/PDF, HEIC in (pillow-heif), SVG out→PNG (resvg/cairosvg),
      compress to a size budget, strip metadata (privacy), batch over a folder or glob
- [ ] charts and diagrams as images: matplotlib (when installed), graphviz / mermaid through `render`
- [ ] optional extras: background removal (rembg), upscaling, local image generation (ComfyUI / SD API)
      when one is configured — never required
- [x] see it where you work: every image/video/document a tool creates appears in the chat — inline image in the
      TUI (textual-image, block fallback), real <img>/<video>/<audio>/PDF viewer in the web UI — plus an
      "Outputs" list of this session's files (open, reveal, copy path)
- Shipped in 0.9.7: the `image` tool (info, edit with 22 steps, create, combine, diff, convert, gif, qr, icons) replaces the
  ImageMagick `image_edit`; files a tool reports (ToolResult.outputs) show under its line in the TUI and web UI.
  Still open here: charts/diagrams as images, the optional rembg/upscale/generator extras, the Outputs list.

### 0.9.8 — video and audio: let text-only models work with media
- [ ] `video` tool on ffmpeg (inventory already finds it) + PyAV: info, trim/cut, concat, convert, compress to a
      size, resize/crop/rotate, speed, mute/extract audio, GIF from a clip, title/text/watermark overlay,
      burn-in subtitles, slideshow from images + music, screen recording to a file
- [ ] "see" a video without a vision model: frames at scene changes → contact sheet + OCR + describe_image
- [ ] audio: info, trim/join/convert/normalise, silence removal; transcription with faster-whisper (local)
      → text/SRT subtitles; speech from text with piper (local) — optional extras
- [ ] viewer: video/audio players in the web UI, a poster frame + duration card in the TUI

### 0.9.9 — Office documents and PDFs (the most-asked automation after Excel)
- [ ] Word (python-docx): read with structure (headings, tables, comments), create from Markdown, fill a
      template's {{placeholders}}, edit text/tables in place keeping styles, docx→PDF (LibreOffice headless
      when installed), compare two versions
- [ ] PowerPoint (python-pptx): deck from an outline (title/bullets/images/charts/notes), read slides and
      notes, replace text, insert images, export slides as images (LibreOffice) so blind models get a card
- [ ] Excel beyond reading: formatting, formulas, charts, conditional formatting, pivots via DuckDB,
      merge/split workbooks, one sheet per group
- [ ] mail merge: template (docx/md/email) × CSV/Excel rows → many documents / PDFs / .eml drafts
- [ ] PDF (pypdf; pymupdf/pdfplumber/ocrmypdf optional): merge/split/reorder/rotate/extract pages, fill and
      read forms, stamp/watermark/number pages, real redaction, compress, tables → CSV, OCR scans, Markdown/
      HTML → PDF (the browser `render` path)
- [ ] conversions through pandoc when installed: md ↔ docx ↔ html ↔ epub ↔ odt
- [ ] viewer: PDF pages, Word/PowerPoint/Excel previews (rendered pages or HTML) in the web UI, page thumbnails
      and a text preview in the TUI

### 0.10.0 — files, OS and system work in Python (works the same on Windows, no bash needed)
- [ ] `files` tool: bulk rename with preview + undo, organise by type/date (photos by EXIF date), duplicates
      by hash, large/old files, folder size tree, checksums, zip/tar/7z create and extract, copy/sync with
      progress — every destructive op previews first and logs an undo record
- [ ] `system` tool (psutil + stdlib): processes (find, kill with approval), ports in use, disk/memory/CPU,
      services, env vars, network checks (DNS, ping, port open, TLS certificate expiry), installed programs
- [ ] email and calendar files: read .eml/.msg/mbox (extract-msg), write .eml drafts, send via SMTP only with
      approval; .ics events and vCards create/read
- [ ] `serve` tool: a local web server for a folder, a built site or a generated report — live reload, LAN URL
      and a QR code for the phone, stops with the session; the web UI previews it in a panel

### 0.10.1 — databases, notebooks, APIs
- [ ] `sql` beyond DuckDB/SQLite: Postgres/MySQL/SQL Server/Oracle through SQLAlchemy, read-only by default,
      schema explorer, ER diagram, query → CSV/Excel/chart, explain plans; connections from the secrets store
- [ ] notebooks: run a notebook (nbclient / papermill with parameters), summarise outputs, clear outputs,
      convert to HTML/PDF/script (nbconvert)
- [ ] `http` tool: call an API with auth from secrets, explore an OpenAPI spec, HTML tables → CSV, RSS
- [ ] data formats: JSON/YAML/TOML/XML/CSV convert and validate (jsonschema), pretty-print, diff

### Later — everyday and engineering helpers (things people pay for or a model can't do)
- [ ] receipts/invoices: OCR → structured fields → spreadsheet; budgets and expense summaries
- [ ] maps: geocode (geopy), distances, folium HTML maps; timezones and meeting-time finder
- [ ] exact maths and units: sympy (algebra, calculus), pint (units), statistics with scipy when installed
- [ ] engineering: dependency audit (pip-audit / npm audit), licence scan, SBOM, code metrics (radon),
      profiling (cProfile / py-spy), JWT decode, cron explain, load test smoke (httpx)
- [ ] offline translation of text and documents (argos-translate) — no tokens, no cloud
- [ ] study aids: flashcards and quizzes → Anki decks (genanki), printable worksheets
- [ ] publish from Markdown: static website, e-book (EPUB), printable booklet — then `serve` it
- [ ] dashboards: an interactive HTML dashboard (plotly offline) from a spreadsheet, served locally
- [ ] printables: barcodes, QR codes, labels, name badges, certificates from a CSV
- [ ] protect a file: encrypt/decrypt with a password (cryptography), secure delete
- [ ] website: "built in — no model needed" section on hexiel.tech showing what hexiel does in Python itself
