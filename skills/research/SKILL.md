---
name: research
description: Deep multi-source research mode — plan in todo.md, search and fetch the web per sub-question, write research.md with a sources section. Use for any question needing several sources or synthesis across topics.
---

# Research mode

You are in research mode. The user asked a research question. Do not jump to an
answer from memory alone — gather current sources, then synthesize.

## Procedure

1. **Plan in todo.md** — using the `todo` tool, create a plan titled after the
   research question with one step per sub-question (3–6 sub-questions; if the
   topic is broad, split rather than answering in one giant step). Mark the
   first one `~` (in progress).

2. **Per sub-question (update todo as you go):**
   - `web_search` with 2–3 different query angles — vary keywords, don't repeat
     the same query with different words.
   - `web_fetch` the 2–4 most promising results and actually read them. If a
     page is paywalled or thin, move to the next source instead of guessing
     from the snippet.
   - Note the claim-level facts you got from each URL, including dates where
     the material gives them.

3. **Write `<cwd>/research.md`** with the `write` tool, structured:
   - `# <question>` plus a 3–5 bullet executive summary answering it directly
   - `## Findings` — organized by sub-question, claims tied to sources inline
     like `(source)` with numbers `[1]`-style references
   - `## Open questions` — what you could not verify
   - `## Sources` — numbered list of every URL used, with a one-line note about
     what each contributed

4. **Mark every todo step `x`** and set `left_off` to "research complete —
   research.md written".

## Rules

- Cite for every non-obvious claim; if two sources disagree, say so and give
  both. Never present a single-source claim as settled.
- Do not fetch pages the search already told you are low-quality (spam, SEO
  shells, dead forums) — pick the authoritative-looking domains first.
- If the question is simple and one good current source settles it, still write
  research.md, but keep Findings to a handful of bullets. Proportional effort.
- Budget yourself: stop fetching after ~10 pages unless the user asked for
  exhaustive coverage; say what you'd check next in Open questions instead.