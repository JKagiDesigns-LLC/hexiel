---
name: refactor-plan
description: Plan a safe refactor in small, individually testable steps before touching the code.
---

# Refactor plan

1. Restate the goal and the constraint that matters (no behaviour change, keep the public API, performance).
2. Find every user of the thing: `grep` for the symbol, check tests, scripts, docs and config. List them.
3. Check the safety net: do tests cover the behaviour? If not, the first step is characterisation tests.
4. Write the plan as numbered steps where each leaves the code working and tests green (add the new path beside the old one, migrate callers, delete the old one). Name the risky step and how to roll it back.
5. Wait for the user's go-ahead before editing; then do one step at a time and run the relevant tests after each.
