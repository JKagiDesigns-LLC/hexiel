---
name: changelog
description: Update CHANGELOG.md in Keep-a-Changelog style from recent commits, without rewriting history.
---

# Changelog

1. Read the top of CHANGELOG.md to learn its format and the last version. If there is none, create one with an `Unreleased` section.
2. List commits since that version (`git log <tag>..HEAD --no-merges`). Group them under Added / Changed / Deprecated / Removed / Fixed / Security.
3. Insert the entries under `## [Unreleased]` (or the new version heading with today's date when the user is releasing), newest first, one line each, linking issues the way earlier entries do.
4. Never edit past releases. Show the diff before saving.
