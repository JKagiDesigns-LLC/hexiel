---
name: regex-helper
description: Build and verify a regular expression against real examples, and explain it in plain words.
---

# Regex helper

1. Ask for (or infer from files) three things: strings that must match, strings that must not, and the flavour (Python, JavaScript, PCRE, grep -E, Java).
2. Write the simplest pattern that works; anchor it; prefer explicit classes over `.*`; avoid nested quantifiers that can backtrack catastrophically.
3. **Test it** — run it in the right language with `bash` on every example, print what matched and which groups. Fix until all cases behave.
4. Explain it piece by piece in one short list and give the final pattern in a code block, with the flags to use.
