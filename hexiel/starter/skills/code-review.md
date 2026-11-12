---
name: code-review
description: Review a diff or branch for bugs, risky changes and missing tests, with findings ranked by severity.
---

# Code review

1. Get the change: `git diff main...HEAD` (or the files/PR the user names). Read the whole diff, then open the surrounding code of anything non-trivial with `read`/`grep` — a diff alone hides callers.
2. Look for, in this order: correctness bugs (wrong condition, off-by-one, unchecked None/null, race, resource leak), security (injection, secrets, unsafe deserialisation, missing authorisation), data loss or irreversible operations, behaviour changes that callers do not expect, then missing or weak tests, then style.
3. For each finding give: file:line, what is wrong, a concrete input that triggers it, and the smallest fix. Drop findings you cannot support with a scenario.
4. End with a verdict (ship / ship after fixes / rework) and the two or three things that matter most. Do not pad with praise or nitpicks.
