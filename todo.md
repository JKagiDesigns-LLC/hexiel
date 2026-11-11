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
- [ ] install.sh (macOS/Linux) + install.ps1 (Windows): uv first (brings Python), pipx, then venv; checksum
      verified; never root; last step runs `hexiel --setup`.
- [ ] hexiel/discover.py — read-only scan, keys masked, nothing written before the review step:
      Claude Code (~/.claude, ~/.claude.json, skills, .mcp.json) · Codex (~/.codex auth.json/config.toml/
      AGENTS.md) · Gemini CLI (~/.gemini) · OpenCode (~/.local/share/opencode/auth.json, ~/.config/opencode)
      · Goose (~/.config/goose, %APPDATA%\Block\goose) · Aider/Continue · Cursor (mcp.json, rules) · Ollama
      (~/.ollama, localhost:11434, cloud sign-in) · shell rc files + ~/.env + Windows user env (registry) ·
      keychains via `keyring` (only entries other agents stored). Windows paths under %USERPROFILE%.
- [ ] Rules: API keys only — subscription OAuth tokens (claude.ai, ChatGPT, Google logins) are detected,
      named, and refused with the API-key / Ollama alternative; keys go to ~/.config/hexiel/auth.toml
      (0600), never config.toml; every finding carries provenance; review table y/n per row.
- [ ] Defaults after import: default profile = first that answers now (loaded local Ollama model → Ollama
      Cloud if signed in → cheapest profile with a working key), proven with one tiny request. What was
      not found → one ADVICE line per provider (sign-up link + exact command); `hexiel auth set NAME`.
- [ ] Re-runnable: `hexiel --setup`, `/import` in the TUI, Setup page in the web UI share the code.
- [ ] Tests: three fake-home fixtures (linux/mac/windows layouts), one per source, "OAuth never written",
      "no leaves config untouched".
- [ ] hexiel.tech #start page redesigned around the two one-liners + "just works out of the box" line.

## Release 0.9.2 — trust: CI, plain-language errors, web parity
- [ ] GitLab CI test stage on push + install smoke matrix (clean Ubuntu/macOS/Windows: install.sh →
      `hexiel doctor --json` → one `-p` prompt). Fix the order-dependent test_escalate failure.
- [ ] Errors in one sentence with the fix: dead server (retry capped and visible), missing sign-in (ADVICE
      line, not raw JSON), crashes ("saved as an incident, run /heal", not a traceback).
- [ ] Web UI: welcome card with example prompts, Setup page (keys, model, budget), Health page.
- [ ] Budget hard stop: `stop_at` per account refuses cloud requests past the budget → local fallback.
- [ ] Website: lead with "ask questions about your spreadsheet or PDF" next to the coding demo (DuckDB,
      openpyxl, pypdf already in the box); 60-second bug-fix demo.

## Release 0.9.3 — Python does the looking: more visibility, fewer tokens (local models first)
- [ ] Symbol index: tree-sitter over the project at startup (refreshed on change, persisted in .hexiel/);
      tools `symbols`/`outline`/`where_is`/`callers`; `read(path, symbol=…)` and `read(path, around=line)`.
- [ ] One `find` tool merging rapidfuzz (typo-tolerant names), BM25 over docstrings/comments, and the
      embedding model when present — one call instead of a grep chain.
- [ ] Vision for non-vision models: `[tool_models] vision = <small ollama vision model>` describes any
      image a non-vision chat model receives (structured card: text with positions, widgets, errors
      verbatim); no-model path = Pillow + tesseract layout + pyzbar; PDF pages rendered only when the text
      layer is empty; render-and-look for HTML/Markdown via a headless browser.
- [ ] Capability inventory: PATH walk cached by PATH hash (formatters, linters, LSPs, tesseract, ImageMagick,
      ffmpeg, pandoc, jq, rg, gh/glab, cloud CLIs, databases, browsers, importable Python packages, OS/GPU/RAM);
      only the relevant slice in the prompt; `have(name)` + the doctor's install line when a tool is
      missing; tool definitions for absent binaries are not sent at all.
- [ ] Structured command output (git status, test results, pip list, ls as data) and a no-model log
      condenser (dedupe, keep errors + tail, "N similar lines removed").
- [ ] Auto-context on the first message (project type, entry points, how to test, recent commits, open
      todos) built locally; diff-aware reads after edits; tool-result cache keyed by args + file mtimes.
- [ ] Deterministic helper tools: date math, units, regex test, JSON path, CSV stats, hash/base64, semver.
- [ ] Performance budget for every item above: measured in tests (ms per call on a 100k-line repo and a
      20k-passage library); anything over budget degrades to "nothing attached", never to a slow turn.

## Deferred (tickets, not built yet)
- [ ] ACP server (serve hexiel into Zed/JetBrains)
- [ ] Agent teams (peer messaging layer on top of the delegate pattern)
- [ ] Skills/plugin marketplace plumbing
