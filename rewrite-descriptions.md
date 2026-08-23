# Task: rewrite dig entry descriptions

You are cleaning up the one-line descriptions in the dig archive. They were
extracted mechanically from a newsletter, so some are fine and some are awkward
fragments (missing a subject, trailing clause, empty author, etc.).

## Input

Read `descriptions.todo.json` in this directory. It is an object:

```json
{
  "98::https://grep.app/": { "title": "Grep", "description": "By Vercel: Fast code search across 1M public GitHub repositories." },
  "9::https://en.wikipedia.org/wiki/Moravec%27s_paradox": { "title": "Moravec's Paradox", "description": "..." }
}
```

The key is an opaque id - **do not change keys**.

## What to do

For each entry, rewrite `description` into **one clean, concise, factual
sentence** that describes the item, using the `title` for context.

- Keep it short (aim for under ~25 words). One sentence.
- Fix fragments: give it a subject, drop dangling leading words, fix an empty
  "By ," author, remove stray punctuation.
- Stay faithful - only use what's in the title/description. **Do not invent
  facts, features, authors, or numbers.**
- Neutral, informative tone. No marketing fluff, no emoji, no "check out".
- If a description is already clean, you may keep it as is.

## Output

Write `descriptions.json` in this directory: an object mapping **the same keys**
to the rewritten string.

```json
{
  "98::https://grep.app/": "Vercel's fast code search across one million public GitHub repositories.",
  "9::https://en.wikipedia.org/wiki/Moravec%27s_paradox": "..."
}
```

Then stop. `build-index.py` merges `descriptions.json` into a separate
`descriptionClean` field (the original `description` is never overwritten), and
`index.html` prefers the cleaned text when present.
