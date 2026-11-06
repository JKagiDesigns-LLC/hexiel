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
- [ ] Terminal autocomplete: paths (@, ~, quoted, first-qualifier), /commands, /model names,
      Ctrl+R fuzzy reverse-history.
- [ ] Cross-harness instruction discovery: .cursor/rules/*, .codex/(AGENTS/rules), GEMINI.md,
      .clinerules, COPILOT.md/.agent.md etc. read alongside CLAUDE.md/AGENTS.md (last wins, layered).
- [ ] Session fork: /fork — branch the conversation from any point into a new session file.
- [ ] Session share: /share — self-contained single-file HTML transcript (no server, no secrets)
      placed next to the session; open or attach anywhere.
- [ ] Watch mode: /watch — hexiel stays alive and reacts to AI:/AI? comments saved in any editor.

## Release 0.8.7 — isolation + accuracy
- [ ] Worktree isolation: /wt (list/create/open), sub+headless runs can run in their own worktree;
      parallel agent runs safe on one repo. Env files copied via post_setup hook (Devin-style).
- [ ] LSP diagnostics tool: pyright/pylsp-based diagnos for python, tsc for TS/JS, go vet, cargo-
      check; surfaced as a read-only `diagnostics` tool + run after edits when available (cheap, local).
- [ ] Formatter integration: black/ruff/prettier/gofmt detected; edits auto-format the touched
      region/format-on-write with formatter="off" escape hatch; instant-checks upgraded.
- [ ] Sandboxing (opt-in safe profile): macOS sandbox-exec profile, Linux bubblewrap/landlock with
      sudo-config note plus container fallback (docker/podman); approval gates bypass only inside.
- [ ] CI runner mode: `hexiel --ci` reads issue/commit context from env (GitLab first), runs
      headless with read-only default, comments results back via API token if provided.

## Release 0.9.0 — zero-to-running for non-technical users
- [ ] One-liner installers: curl -fsSL https://hexiel.tech/install.sh | sh  (macOS/Linux; uv-less)
      PowerShell: irm https://hexiel.tech/install.ps1 | iex (Windows). Bootstrap python if absent
      (winget/dev-tools/apt guidance), create venv, pipx-style install, first-run config wizard.
- [ ] `/import` auto-detector on first run: finds ollama (running? models? ) + keys (claude.json,
      .codex/auth.json, openai/anthropic env vars), MCP configs (.claude.json, .mcp.json, .gemini),
      saved sessions/commands/hooks/skills; imports with clear labels + a review step.
      Reuses existing CLAUDE.md/AGENTS.md reading.
- [ ] hexiel.tech #start page redesigned around the two one-liners + "just works out of the box" line.

## Deferred (tickets, not built yet)
- [ ] ACP server (serve hexiel into Zed/JetBrains)
- [ ] Agent teams (peer messaging layer on top of the delegate pattern)
- [ ] Skills/plugin marketplace plumbing
