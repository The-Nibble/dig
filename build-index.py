#!/usr/bin/env python3
"""Heuristic Nibble export -> data/nibble.json.
NOT the canonical parser: deterministic regex, best-effort tagging. Meant to be
overwritten wholesale by the real parser. Prints a report to stderr.

This only parses the Substack archive. It does NOT touch index.html - merging
the sources, deduping and inlining is build-page.py's job. The split exists
because the Substack export is a manual download that only lives on a laptop,
while the Discord half refreshes on a schedule and has to rebuild the page
without it. data/nibble.json is committed for exactly that reason.

Usage:
  python3 build-index.py     # parse ~/Downloads/nibble-archive -> data/nibble.json
  python3 build-page.py      # then merge + inline into index.html
"""
import csv, glob, os, re, json, html, hashlib, sys
from collections import Counter, defaultdict
from urllib.parse import urlparse
from taxonomy import KIND_META, TAG_RULES, TAG_META, entity, tags_for, tidy_desc

D = os.path.expanduser('~/Downloads/nibble-archive')
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'nibble.json')

# ---- roster ----
slug = {}
for f in glob.glob(D + '/posts/*.html'):
    b = os.path.basename(f)[:-5]; nid, _, sl = b.partition('.'); slug[nid] = (sl, f)

def ednum(sl):
    m = re.fullmatch(r'nibble-(\d+)', sl)
    if m: return int(m.group(1))
    if re.fullmatch(r'0*\d+', sl): return int(sl)
    return None

editions_meta = {}  # ed -> dict
with open(D + '/posts.csv') as fh:
    for r in csv.DictReader(fh):
        nid = r['post_id'].split('.')[0]
        if r['is_published'] != 'true' or nid not in slug: continue
        sl, path = slug[nid]
        ed = ednum(sl)
        if ed is None or ed < 1 or ed > 100:   # skip #0 pilot + special posts
            continue
        editions_meta[ed] = {'number': ed, 'slug': sl, 'title': r['title'].strip(),
                             'publishedAt': r['post_date'], 'path': path}

# ---- section -> kind ----
def norm(t):
    t = re.sub(r'<[^>]+>', '', t)
    t = html.unescape(t)
    t = t.lower().replace("'", '').replace('’', '').replace('(', '').replace(')', '')
    return re.sub(r'[^a-z0-9]+', ' ', t).strip()

KIND_MAP = {}
def addmap(kind, *names):
    for n in names: KIND_MAP[n] = kind
addmap('news', 'whats happening', 'news', 'catch up', 'catch up with the tech', 'catching up',
       '0x digest', 'agi digest', 'ai digest', 'dev design digest', 'dev digest',
       'code editor updates', 'new model drops', 'new model and data drops')
addmap('awe', 'wild world web', 'what brings us to awe', 'what brings us two awe', 'cool stuff',
       'something to think about')
addmap('til', 'tils', 'today i we learnt', 'today iwe learnt', 'today i learned moments')
addmap('read', 'off topic reads watches', 'off topic reads listens', 'some off topic reads watches',
       'recommendations', 'gadget reccos', 'what we have been consuming')
addmap('tool', 'builders nest', 'cool oss projects', 'new tools in town', 'new in town',
       'what we have been trying', 'what we have been trying reading', 'tools recommendations',
       'useful links', 'playgrounds')
addmap('quote', 'wisdom bits', 'ponder worthy words', 'quote of the week',
       'a quote that got me thinking')

EXCLUDE = {'meme of the week', 'weekly standup', 'wallpaper of the week', 'from the authors laptop',
           'note', 'where do we stand in the year', 'where do we stand in the year powered by year progress bot',
           'what are we up to', 'what weve been up to', 'things i have been up to', 'shoutout',
           'friends of nibble', 'job posts', 'fin bits', 'watching'}

FURNITURE = re.compile(r'(^|\.)(wow|a|p|latest|why|files)\.nibbles\.dev$|nibbles\.dev/(subscribe|survey)|notebooklm\.google\.com|/subscribe|/survey', re.I)

def clean_text(frag):
    return html.unescape(re.sub(r'<[^>]+>', '', frag)).replace('‍', '').strip()

EMOJI = re.compile(r'^(?:[^\w\s,.:;"\'-]|️|‍|\U0001F3FB-\U0001F3FF)+')

def marker_of(text):
    m = re.match(r'^\s*([\U0001F000-\U0001FAFF←-⯿☀-➿️‍]+)', text)
    return m.group(1).strip('‍️ ') if m else None

def split_sections(body):
    """yield (level, raw_heading, content_html) in document order."""
    heads = list(re.finditer(r'<h([1-6])[^>]*>(.*?)</h\1>', body, re.S))
    for i, m in enumerate(heads):
        start = m.end()
        end = heads[i+1].start() if i+1 < len(heads) else len(body)
        yield int(m.group(1)), m.group(2), body[start:end]

def parse_units(content):
    """ordered list of (kind_of_container, inner_html) for <li> and <blockquote>."""
    out = []
    for m in re.finditer(r'<li[^>]*>(.*?)</li>|<blockquote[^>]*>(.*?)</blockquote>', content, re.S):
        out.append(m.group(1) if m.group(1) is not None else m.group(2))
    return out

def parse_quote(inner):
    lines = [clean_text(x) for x in re.split(r'<br\s*/?>', inner)]
    lines = [x for x in lines if x]
    if not lines: return None, None
    attribution = None
    if len(lines) >= 2 and re.match(r'^\s*[—–―~‒-]', lines[-1]):
        attribution = re.sub(r'^[\s—–―~‒-]+', '', lines[-1]).strip()
        quote = ' '.join(lines[:-1])
    else:
        quote = ' '.join(lines)
        parts = QUOTE_SEP.split(quote, maxsplit=1)
        if len(parts) > 1:
            quote, attribution = parts[0], parts[1].strip()
    mk = marker_of(quote)
    if mk: quote = quote[len(mk):]
    return quote.strip().strip('"“”'), (attribution.strip('"“”') if attribution else None)

report = {'unknown': Counter(), 'unknown_eds': defaultdict(set), 'noitem_sections': 0,
          'skipped_furniture': 0, 'low_conf': 0}
entries = []
eid = 0
QUOTE_SEP = re.compile(r'\s+[~–—―‒]\s+|\s+--\s+')

# descriptions are the text after the first link, so they often start with a
# stray '.', ',', '?' or a dangling 'and/but'. tidy that mechanically.
for ed in sorted(editions_meta):
    meta = editions_meta[ed]
    body = open(meta['path']).read()
    date = (meta['publishedAt'] or '')[:10]
    secs = list(split_sections(body))
    if not secs:
        continue
    minlvl = min(s[0] for s in secs)
    cur_kind = None
    cur_heading = None
    crossrefs = set()
    for m in re.finditer(r'nibbles\.dev/(?:p/)?(\d+)', body):
        n = int(m.group(1))
        if 1 <= n <= 100 and n != ed: crossrefs.add(n)
    ecount0 = len(entries)
    for lvl, raw_h, content in secs:
        n = norm(raw_h)
        raw_h_clean = clean_text(raw_h)
        if n in EXCLUDE:                         # excluded at any level
            cur_kind = None
        elif n in KIND_MAP:                      # known section at any level
            cur_kind = KIND_MAP[n]
        elif lvl == minlvl:                      # unknown top-level section: park in awe
            cur_kind = 'awe'
            report['unknown'][n] += 1; report['unknown_eds'][n].add(ed)
        # else: unknown sub-heading -> inherit current kind (digest editorial heads)
        cur_heading = raw_h_clean
        # news is temporal — parsed for structure/cross-refs, but not indexed
        if cur_kind is None or cur_kind == 'news':
            continue
        units = parse_units(content)
        for li in units:
            if cur_kind == 'quote':
                title, attribution = parse_quote(li)
                if not title: continue
                e = {'id': 0, 'ed': ed, 'kind': 'quote', 'timeless': True,
                     'heading': cur_heading, 'title': title, 'description': ''}
                if attribution: e['attribution'] = attribution
                e['tags'] = ['quote']; e['date'] = date
                entries.append(e); continue
            # non-quote: first anchor is the entity
            am = re.search(r'<strong>\s*<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', li, re.S) \
                 or re.search(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', li, re.S)
            if not am:
                continue
            url = html.unescape(am.group(1)).strip()
            title = clean_text(am.group(2))
            if not title: continue
            # Substack renders footnote markers as <a href="#footnote-1">1</a>.
            # They point nowhere outside the edition and are not links anyone
            # would search for, so they never become entries.
            if not re.match(r'https?://', url): continue
            before = li[:am.start()]
            before_txt = clean_text(before)
            marker = marker_of(before_txt if before_txt else clean_text(li))
            if marker and before_txt.startswith(marker): before_txt = before_txt[len(marker):]
            before_txt = re.sub(r'[\s\[\(<–—:-]+$', '', before_txt).strip()
            after = clean_text(li[am.end():])
            # links at the end of a sentence have no trailing text; use the lead-in
            desc = tidy_desc(after) or tidy_desc(before_txt)
            host, repo = entity(url)
            # furniture guard: skip only if sole substance is a furniture link
            if host and FURNITURE.search(host + urlparse(url).path) and not desc:
                report['skipped_furniture'] += 1; continue
            label, timeless = KIND_META[cur_kind]
            tags = tags_for(cur_kind, title, desc, host, url, repo)
            e = {'id': 0, 'ed': ed, 'kind': cur_kind, 'timeless': timeless,
                 'heading': cur_heading, 'title': title, 'description': desc}
            if marker: e['marker'] = marker
            e['url'] = url
            if host: e['domain'] = host
            if repo: e['repo'] = repo
            e['tags'] = tags; e['date'] = date
            entries.append(e)
    meta['crossRefs'] = sorted(crossrefs)
    meta['entryCount'] = len(entries) - ecount0
    meta['contentHash'] = hashlib.blake2b(body.encode(), digest_size=8).hexdigest()

# assign ids in order
for i, e in enumerate(entries, 1): e['id'] = i

# --- optional AI description polish (opencode) -----------------------------
# stable key so a rewrite survives re-parsing; cleaned text goes to a SEPARATE
# field (descriptionClean) and never overwrites the deterministic ground truth.
_HERE = os.path.dirname(os.path.abspath(__file__))
def _key(e): return f"{e['ed']}::" + (e.get('url') or ('q:' + e['title']))
_clean_path = os.path.join(_HERE, 'descriptions.json')
if os.path.exists(_clean_path):
    _clean = json.load(open(_clean_path, encoding='utf-8'))
    for e in entries:
        c = _clean.get(_key(e))
        if c: e['descriptionClean'] = c
# write the to-rewrite list for an opencode sub-agent (intermediate, gitignored)
_todo = {_key(e): {'title': e['title'], 'description': e['description']}
         for e in entries if e.get('description')}
json.dump(_todo, open(os.path.join(_HERE, 'descriptions.todo.json'), 'w', encoding='utf-8'),
          ensure_ascii=False, indent=2)

# ---- emit ----
# Tag aggregation, dedupe and the entity count all move to build-page.py: they
# are properties of the MERGED index, and this script only sees one source.
editions_out = []
for ed in sorted(editions_meta):
    m = editions_meta[ed]
    editions_out.append({'number': m['number'], 'slug': m['slug'], 'title': m['title'],
                         'url': 'https://nibbles.dev/p/' + m['slug'],
                         'publishedAt': m['publishedAt'], 'crossRefs': m['crossRefs'],
                         'entryCount': m['entryCount'], 'contentHash': m['contentHash']})

for e in entries:
    e['src'] = 'nibble'

from datetime import datetime, timezone
out = {'generatedAt': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
       'editions': editions_out, 'entries': entries}
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, 'w', encoding='utf-8') as fh:
    json.dump(out, fh, ensure_ascii=False, separators=(',', ':'))

# ---- report ----
def pr(*a): print(*a, file=sys.stderr)
pr(f"\n=== NIBBLE PARSE REPORT ===")
pr(f"wrote {OUT} ({os.path.getsize(OUT)/1024:.1f} KB)")
pr(f"editions indexed: {len(editions_out)} (#{editions_out[0]['number']}..#{editions_out[-1]['number']})")
pr(f"entries: {len(entries)}")
pr("by kind: " + str(dict(Counter(e['kind'] for e in entries))))
pr(f"skipped furniture links: {report['skipped_furniture']}")
pr(f"\nUNKNOWN section-level headings (parked in awe, {len(report['unknown'])} distinct):")
for n, c in report['unknown'].most_common():
    eds = sorted(report['unknown_eds'][n]); pr(f"  {c:3d}x  {n!r:45s} eds {eds}")
low = [e for e in entries if not e.get('url') and e['kind'] != 'quote']
pr(f"\nentries with no url (non-quote): {len(low)}")
pr("\nnext: python3 build-page.py")
