# Contributing to hexiel

Anyone can use, study, and improve hexiel; if you improve it, please send the
improvement back as a merge request. Selling hexiel is **not** permitted — the
license is GPLv3 with the Commons Clause (see `LICENSE`).

## Reporting bugs

If hexiel misbehaves, it usually captured the failure itself:

1. Check `.hexiel/incidents/*.json` in the project directory — each incident
   has the error, a traceback, and the context at the time.
2. Run `hexiel --doctor` and include anything it flags.
3. Open an issue with the incident JSON attached (it contains no code content,
   only messages/args of the failing call).

Better yet — hexiel can heal itself:

```
/heal        # inside hexiel: runs the test suite, fixes, and proposes an MR
```

## Proposing changes (the hexiel way)

hexiel has its own MR workflow built in — you can contribute *with* the tool:

1. Fork the repo (GitLab: "Fork" button) and clone *your* fork.
2. Configure a GitLab Personal Access Token with `api` scope:
   `hexiel` then `/setup-gitlab <token>` — or set `HEXIEL_GITLAB_TOKEN`.
3. Work normally; the agent will suggest using the gitlab tool to propose changes.
   It will ask your approval for each step: create branch → commit → push to
   your fork → open a merge request with `source_project_id` set, so the MR is
   attributed to you and lands on the upstream project for review.
4. In GitLab, the owner's review is required to merge — nothing auto-merges,
   by design (hexiel never merges its own MRs).

## Local development

```
git clone <your-fork> && cd hexiel
python -m venv .venv && .venv/bin/pip install -e 'hexiel[serve]'
.venv/bin/hexiel --doctor          # environment check
.venv/bin/pytest                   # run the test suite
```

- Keep tests green: `pytest` must pass before you propose anything. The `test`
  tool inside hexiel drives the runners detected for your project (pytest,
  vitest/jest/npm, go test, cargo test, make test).
- Plugin ideas that touch provider/model logic: add a profile to `config.toml`
  instead of new code when possible.
- OpenWebUI plugin authors: drop plugin files in `plugins/` — no repo changes
  needed.

## Code conventions

- Python 3.11+, stdlib-first; httpx for HTTP, textual for the TUI, aiohttp for
  the web UI. No heavyweight frameworks.
- Every feature lands with tests in `tests/`.
- Token economy matters: prefer local (Python) computation over feeding raw
  output to the model — cap tool outputs, cache, distill before the model sees
  it (see README: "What runs locally vs. what costs tokens").