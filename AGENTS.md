# AGENTS.md — working rules for AI agents in this repo (hexiel)

hexiel is a Python coding agent (PyPI: hexiel). Tests: `.venv/bin/python -m pytest` (the system
python lacks deps; pre-commit runs the same suite — budget ~5 min per commit). Config and data
are sandboxed per test by tests/conftest.py.

## Commit authorship (required)

The human owner (Josh, `josh@jkagidesigns.com`) is the author of every commit. Agents work on
their behalf:

- Never add AI co-author trailers (`Co-authored-by: Claude`, GLM, OpenCode, or any model) and
  never claim the agent, Claude, GLM, or any AI as the author of changes in commit messages,
  tickets, docs, or notes.
- Keep the repo's `user.name`/`user.email` as they are; do not override author/committer env.
- Write commit messages factually about what changed and reference the ticket id, e.g.
  `Busy notes name the actual holder — ticket e1951eb951e8 (fixed in 0.5.0)`. First person is
  fine and describes the change, not the author.

## Repo workflow

- Tickets live on the website (remote.hexiel.tech relay; queue: `POST /api/reports`,
  kind = error | bug | feature). Create them via the public endpoint; resolve them with proof via
  the relay repo's resolver:
  `cd /mnt/storage/code/Private/applications/hexiel-cloud && .venv/bin/python scripts/resolve_ticket.py
  <id-prefix> --fixed-in X.Y.Z --resolution "..." --comment "..." --proof shot.png:"Caption"`
  (admin key in `~/.config/hexiel/relay-admin.key`). Public tickets auto-mirror to GitLab issues.
- Releases: bump `hexiel/__init__.py` + `pyproject.toml`, commit, tag `vX.Y.Z`, push main and the
  tag; CI publishes to PyPI. NEVER chain `git tag` after a commit without checking it succeeded
  (the pre-commit hook can rewrite `.secrets.baseline` — re-stage and commit again). Auto mode
  blocks pushing release tags and production deploys: give the user a `!` command instead.
- After pushing to `origin` (GitLab), mirror to GitHub: `scripts/push_github.sh` (pushes main +
  tags; token comes from /mnt/storage/code/.env as a push-time header, never stored).
- Pre-commit hook: secret scan + bandit + full test suite — never edit files while a commit's
  hook suite is running (pytest reads the working tree).