#!/usr/bin/env bash
# Point git at the versioned hooks in .githooks/ (secret scan + bandit + tests on every commit).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
git config core.hooksPath .githooks
chmod +x .githooks/*
echo "hooks installed. Needs dev tools:  pip install -e '.[dev]'"
