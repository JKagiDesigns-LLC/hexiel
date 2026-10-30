#!/usr/bin/env bash
# Mirror-push to the GitHub repo (JKagiDesigns-LLC/hexiel) after pushing to GitLab.
#
#   scripts/push_github.sh [refs...]       # default: main + all tags
#
# The token never touches disk or .git/config: it is read from /mnt/storage/code/.env at push
# time and sent as an Authorization header, so `git remote -v` and git's own output stay clean.
# Run this right after `git push origin main` (and after every tag push) to keep GitHub in sync.
set -euo pipefail

GH_REPO="${GH_REPO:-JKagiDesigns-LLC/hexiel}"
ENV_FILE="${HEXIEL_ENV:-/mnt/storage/code/.env}"

TOKEN=$(sed -n 's/^GITHUB_PERSONAL_ACCESS_TOKEN=//p' "$ENV_FILE" | head -1 | tr -d '"\r' | xargs)
[ -n "$TOKEN" ] || { echo "no GITHUB_PERSONAL_ACCESS_TOKEN in $ENV_FILE" >&2; exit 1; }

AUTH=$(printf 'x-access-token:%s' "$TOKEN" | base64 -w0)
if [ $# -gt 0 ]; then
  exec git -c "http.extraheader=Authorization: Basic $AUTH" push "https://github.com/${GH_REPO}.git" "$@"
fi
exec git -c "http.extraheader=Authorization: Basic $AUTH" push "https://github.com/${GH_REPO}.git" main --tags