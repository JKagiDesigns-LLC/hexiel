---
name: explain-this-repo
description: Explain an unfamiliar codebase: what it does, how it is laid out, how to run it and where to start reading.
---

# Explain this repo

1. Call the `map` tool once for the tree, then read the README, the manifest (package.json, pyproject.toml, pom.xml, go.mod …) and any docs/ or AGENTS.md.
2. Find the entry points (main, CLI, server start, routes) and follow one real request or command from entry to output with `grep` and `read`.
3. Report, briefly: purpose (one paragraph), layout (a short tree with one line per important folder), how to install/run/test (commands that you checked exist), the main data flow, external services it needs, and the three files to read first.
4. Say what you could not determine instead of guessing.
