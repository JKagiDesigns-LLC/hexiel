---
name: sql-explainer
description: Explain a SQL query step by step, find performance problems, and suggest a safer or faster version.
---

# SQL explainer

1. Read the query and any schema or migration files that define the tables and indexes it touches.
2. Explain it in execution order (FROM/JOIN → WHERE → GROUP BY → HAVING → SELECT → ORDER BY → LIMIT) in plain words, naming what each join does to row counts.
3. Flag: missing join conditions (accidental cross joins), functions on indexed columns, `SELECT *`, `NOT IN` with NULLs, N+1 patterns, missing indexes, and anything that writes or deletes without a WHERE.
4. If a database is available through the `data` tool or a client, run `EXPLAIN` (never run writes). Offer a rewritten query and say what result it must still equal.
