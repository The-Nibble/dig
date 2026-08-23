# dig

A searchable index of [The Nibble](https://nibbles.dev) — the timeless bits
(tools, TILs, reads, quotes) pulled out of every edition of the newsletter, with
the week's news left behind. Answers "when did we first and last talk about X"
for any tag or search term.

Live at **https://dig.nibbles.dev**

## Hosting

`index.html` is a single self-contained static file — the full index is inlined,
all logic is vanilla JS, no build step or backend at runtime. It's served by
GitHub Pages (see `CNAME`). To ship: commit and push.

## Regenerating the index

The data is produced from a Substack export by a **heuristic** parser
(`build-index.py`) — deterministic regex, best-effort tagging. It is *not* a
canonical system of record; a future canonical parser can overwrite the inlined
data wholesale.

```sh
# 1. Substack -> Settings -> Exports -> download, unzip to ~/Downloads/nibble-archive/
#    (expects posts/ of {id}.{slug}.html + posts.csv)
# 2. inline the fresh index straight into index.html:
python3 build-index.py

# optional: also write the raw index as JSON
python3 build-index.py --json index.json
```

`build-index.py` rewrites the `const BOOTSTRAP = …` line in `index.html` in
place, so the page stays self-contained.

## What it does / doesn't

- **Editions #1–#100.** News is excluded (temporal); tools, curiosity, TILs,
  reads and quotes are kept. Unknown section headings are parked in Curiosity and
  reported, never dropped.
- **Search is plain-English-ish**: token/prefix matching plus a concept-synonym
  layer, so `rag` reaches vector stores, embeddings and LangChain even when an
  entry never says "rag". No embeddings/model — deterministic and offline.
- **First/last trace** works for any tag chip *and* any free-text query.
- URL reflects state (`#q=…`, `#tag=…`), so a first/last view is a shareable link.

## Descriptions and edition links

Each entry links to its source, and its edition number links back to that edition
on Substack (`nibbles.dev/p/<slug>`). This is edition-level, since Substack has no
reliable per-line anchor.

Descriptions are extracted as the text after the first link, so `build-index.py`
tidies them deterministically (strips stray leading punctuation and a dangling
"and"/"but", capitalizes, adds a full stop). For an extra polish pass, an
**opencode** sub-agent can rewrite them:

```sh
python3 build-index.py                          # also writes descriptions.todo.json
opencode run "$(cat rewrite-descriptions.md)"   # writes descriptions.json
python3 build-index.py                          # merges + re-inlines into index.html
```

The rewritten text goes to a separate `descriptionClean` field and the page
prefers it; the deterministic `description` (ground truth) is never overwritten.
`descriptions.json` is committed; `descriptions.todo.json` is an intermediate.
