---
name: data-cleaner
description: Clean a messy CSV/Excel file: find problems first, fix them reproducibly, keep the original, report what changed.
---

# Data cleaner

1. Profile before touching: use the `data` tool (DuckDB SQL over the file) for row count, column types, nulls per column, duplicate rows/keys, odd values (`DISTINCT` on short columns), date and number formats, leading/trailing spaces, mixed case.
2. List the problems found and the fix for each; ask about anything ambiguous (which duplicate wins, how to read 03/04/2026).
3. Apply fixes with SQL or a short script, writing to a **new** file (`name-clean.csv`/`.xlsx`) — never overwrite the original. Keep the script so the cleaning can be re-run.
4. Report before/after counts, what was dropped or changed, and anything left for a human to decide.
