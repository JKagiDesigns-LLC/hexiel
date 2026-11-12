---
name: release-notes
description: Turn commits or merged changes since the last release into user-facing release notes.
---

# Release notes

1. Find the range: `git describe --tags --abbrev=0` for the last tag, then `git log <tag>..HEAD --no-merges --format='%h %s'`. Read bodies of unclear commits.
2. Group into: New, Improved, Fixed, Changed/Breaking, and Security. Write each line from the user's point of view ("Export now keeps column order"), not the code's ("refactor exporter").
3. Put breaking changes and migration steps first, with the exact action the user must take.
4. Leave out internal-only commits (CI, formatting, tests) unless they change behaviour. Keep ticket numbers the project uses. Output Markdown ready to paste into a release page.
