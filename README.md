<p align="center">
  <img src="docs/logo.svg" alt="hexiel logo — angel wings around an H" width="180">
</p>

<h1 align="center">hexiel</h1>

<p align="center">
  <b>An open-source AI coding agent for your terminal and browser — a guardian angel for your codebase.</b><br>
  Runs on local models (Ollama, even on a mini-PC iGPU) or any big cloud model.<br>
  A free, self-hostable alternative to Claude Code and opencode.
</p>

<p align="center"><b>🌐 Website:</b> <a href="https://hexiel-f64d26.gitlab.io/">hexiel-f64d26.gitlab.io</a> · designed by <a href="https://www.jkagidesigns.com">JKagiDesigns LLC</a></p>

<p align="center">
  <img src="docs/screenshots/tui-welcome.png" alt="hexiel terminal UI welcome screen" width="49%">
  <img src="docs/screenshots/tui.png" alt="hexiel fixing a bug in the terminal UI" width="49%">
</p>

## Why hexiel

- **Built for modest hardware.** Everything that doesn't need a model (searching,
  mapping the project, waiting on tests, distilling web pages, batching work in a
  `python` script) runs locally in Python. The prompt is kept lean: on an Intel iGPU
  mini-PC running `qwen3.6:35b-a3b-coding`, each request's prompt processing
  dropped from 46.5 s to 23.7 s in v0.2.6.
- **Any model, local or cloud.** Ollama (local or Cloud), Anthropic, OpenAI,
  Gemini, xAI, OpenRouter, DeepSeek, or any OpenAI-compatible server (vLLM,
  LM Studio, TGI). Switch per session or permanently, from the UI.
- **Terminal *and* browser.** A full-screen TUI, a browser UI, one-shot headless
  runs, and an OpenAI-compatible API — one agent core behind all of them.
- **Brings your ecosystem.** Your existing Claude Code skills (`~/.claude/skills`)
  and OpenWebUI tool/pipe/filter plugins work as-is.
- **Remembers and keeps track.** File-based memory (`MEMORY.md`), a `todo.md`
  that survives crashes, auto-compaction at a Claude-Code-sized context window.
- **Fixes itself.** Crashes become incident files; `/heal` reproduces, fixes,
  tests and proposes a merge request — you approve it.
- **Free to use, can't be resold.** GPLv3 + Commons Clause.

## Quickstart

```bash
git clone https://gitlab.com/jkagidesigns1/public/applications/hexiel.git
cd hexiel
python -m venv .venv
.venv/bin/pip install -e '.[serve]'     # Windows: .venv\Scripts\pip install -e ".[serve]"
.venv/bin/hexiel --doctor               # preflight checks with copy-paste fixes
.venv/bin/hexiel                        # start the TUI
```

Then put it on your PATH so it runs from any project:

```bash
ln -s "$PWD/.venv/bin/hexiel" ~/.local/bin/hexiel
cd ~/code/your-project && hexiel
```

hexiel always treats **the directory you launch it from as the project**: it
detects that tree's toolchain, reads its skills and keeps its state
(`.hexiel/` — todo, memory, incidents, web cache) right there. The header shows
the hexiel version and the project you're in; `hexiel -c` resumes the last
conversation *of this project*.

**Windows:** works out of the box — the agent shells through PowerShell (`pwsh`,
`winget install Microsoft.PowerShell`), and `bash` falls back to it when no
WSL/git-bash is on PATH.

### Ways to run it

```bash
hexiel                    # full-screen terminal UI
hexiel --serve            # browser UI at http://127.0.0.1:8777
hexiel -p "explain this repo in one paragraph"   # one-shot, headless
hexiel -c                 # resume this project's last conversation
hexiel -m sonnet          # pick a model profile for this run
```

## Using it

| | |
|---|---|
| <img src="docs/screenshots/tui-menu.png" alt="slash command menu"> | <img src="docs/screenshots/tui-config.png" alt="settings editor"> |
| **Type `/`** for the command menu — every command and skill with what it does; ↑/↓, Tab to complete, Enter to run. `/model ` lists your profiles. | **`/config`** shows every setting in a table. Pick a row, pick a value, then choose **this session** or **save permanently** (one line of `config.toml` changes; comments are kept). |

- **Screenshots:** type an image path in your message (`why does @shot.png look
  broken?`, `~/Pictures/error.jpg`) and it's attached for vision models.
- **Activity line:** while hexiel works, the bottom-left shows what it's doing —
  thinking, writing, running your tests — with elapsed time and tokens.
  `Ctrl+C` stops a turn.
- **Approvals:** edits and commands ask first (`y` / `a`lways / `n`); `/auto`
  goes hands-free.
- **Updates:** hexiel checks for new releases in the background. `/update` (or
  the web UI's update button) installs it and restarts, resuming your conversation.
  It refuses to update a checkout with uncommitted changes.

### Browser UI

`hexiel --serve` runs the same agent in a browser: streamed markdown, tool-call
cards, approval dialogs, model picker, ⚙ settings, context gauge, todo, memory
and sessions. Local-only by default; `--host 0.0.0.0` prints a loud warning
(anyone who can reach the port can run commands on your machine).

| | |
|---|---|
| <img src="docs/screenshots/web.png" alt="hexiel browser UI"> | <img src="docs/screenshots/web-settings.png" alt="browser settings panel"> |

## Models

Profiles live in `~/.config/hexiel/config.toml` (written on first run). Switch
with `/model NAME`, `hexiel -m NAME`, or the settings table.

```toml
default = "local"

[models.local]                      # Ollama on this machine or a home server
provider = "openai"
base_url = "http://localhost:11434/v1"
model = "qwen3.6:35b-a3b-coding"    # coding + screenshots + tool calling in one model
context_window = 65536              # match the server's OLLAMA_CONTEXT_LENGTH
extra_body = { reasoning_effort = "none" }   # skip slow "thinking" on modest hardware

[models.sonnet]
provider = "anthropic"
model = "claude-sonnet-5-5"
api_key_env = "ANTHROPIC_API_KEY"
```

**Picking a local model:** on low-end hardware, prefer mixture-of-experts
models with ~3B active parameters (they run several times faster than dense
models of the same size) that have vision + tool-calling. `qwen3.6:35b-a3b-coding`
passed all of hexiel's checks (tool calls, screenshots, a compiled Java task) on
an Intel iGPU at ~12 tokens/s. Hybrid "thinking" models should run with
`reasoning_effort = "none"`; otherwise one step can take minutes.

## What hexiel does

### Tools
`bash` · `python` (batch many steps into one local script) · `read` · `write` ·
`edit` · `glob` · `grep` · `list` · `map` (cached project tree) · `file_info` ·
`powershell` · `test` (your detected runner) · `container` (docker/podman +
compose) · `view_image` / `image_edit` · `web_search` / `web_fetch` (no API key
needed) · `memory` · `todo` · `gitlab_mr` · `skill` — plus any OpenWebUI plugin
in `plugins/`.

### Context discipline
- **Auto-compaction, Claude-Code style:** at 82% of the context window, older
  history is summarized (task, recent work, decisions, errors, next steps) and
  the conversation continues. Real provider usage numbers drive it.
- **Local-first token economy:** tool output is budgeted and distilled in Python
  before the model sees it.

| Work | Where it runs |
|---|---|
| Toolchain discovery, project map, file indexing | local (Python) |
| Multi-step reads/searches/parsing | one `python` script instead of many model round-trips |
| Long command output | head + tail kept, middle elided |
| Waiting for tests / containers / builds | local; only the verdict + failures reach the model |
| Duplicate tool calls in a turn | answered from cache |
| Web pages | distilled to text; full text parked on disk for follow-up reads |
| Old tool output and images in long sessions | dropped locally before any summarization call |
| Skills | one-line index; full instructions load only when used |
| Reasoning, planning, writing code | the model — spend tokens there, nowhere else |

### Project instructions, todo and memory — maintained automatically
- **Project instructions:** `AGENTS.md` / `CLAUDE.md` / `HEXIEL.md` (any letter
  case) at the project root are loaded into every session; a root `todo.md` is
  pointed out so the model reads it when you say "continue".
- **todo.md:** multi-step work is tracked in `<project>/.hexiel/todo.md` (`- [ ]`
  queued, one `- [~]` in progress, `- [x]` done). When a request takes three or
  more tool calls and the model hasn't planned it itself, hexiel logs it as a
  task, refreshes the *where we left off* line after every step and ticks it off
  when the turn completes — in Python, at zero token cost. If hexiel or your
  laptop dies mid-task, `hexiel -c` resumes from an accurate checkpoint.
- **Memory:** `~/.local/share/hexiel/memory/` (global) and
  `<project>/.hexiel/memory/` (project), markdown notes indexed by `MEMORY.md`.
  The model saves what it learns; and every auto-compaction also extracts
  *durable facts* (build/test commands, conventions, your preferences) into
  `auto-facts.md` — deduplicated, no extra model call.
- Skills are `SKILL.md` folders from `~/.local/share/hexiel/skills/`, the
  project, **and `~/.claude/skills/`** — Claude Code skills work unchanged.

### Self-healing
Exceptions and provider errors are recorded to `.hexiel/incidents/`. `/heal`
runs the tests, reproduces, fixes the root cause, re-tests, commits on
`hexiel-fix/<slug>` and proposes a merge request. It never pushes to your
default branch and never merges.

### GitLab collaboration (`/ship`, `/heal`, `gitlab_mr`)
- **Owner flow:** turn uncommitted work into a merge request (branch → commit →
  push → MR), approving each step.
- **Contributor flow:** hexiel forks the repo, pushes to *your* fork and opens
  an MR upstream — attributed to you.
- Auth via `HEXIEL_GITLAB_TOKEN` / `GITLAB_TOKEN` or `/setup-gitlab <token>`.
  gitlab.com and self-hosted.

### OpenWebUI compatibility
1. **Use hexiel from OpenWebUI** (or any OpenAI client): point a connection at
   `http://127.0.0.1:8777/v1/chat/completions`. Read-only tools stay available;
   write tools are disabled over the API.
2. **Use OpenWebUI plugins in hexiel:** drop `Tools` / `Pipe` / `Filter` plugin
   files into `plugins/` — methods become tools, `Valves` are honored.

## Contributing

Fork → branch → merge request; the maintainer reviews and approves. hexiel can
do the whole flow for you (`/ship`). See [CONTRIBUTING.md](CONTRIBUTING.md).

```bash
.venv/bin/pytest                    # 62 tests: tools, providers, compaction, TUI,
                                    # settings editor, updater, web UI, plugins
.venv/bin/python scripts/screenshots.py   # regenerate these screenshots
```

## License

GPLv3 with the [Commons Clause](LICENSE): free to use, study, fork and
contribute — **not** free to sell as a product or service.
