#!/usr/bin/env python3
"""Heuristic Nibble export -> self-contained dig/index.html.
NOT the canonical parser: deterministic regex, best-effort tagging. Meant to be
overwritten wholesale by the real parser. Prints a report to stderr.

Usage:
  python3 build-index.py                 # inline the index into index.html
  python3 build-index.py --json out.json # also write the raw index JSON
"""
import csv, glob, os, re, json, html, hashlib, sys, gzip
from collections import Counter, defaultdict
from urllib.parse import urlparse

D = os.path.expanduser('~/Downloads/nibble-archive')
_args = sys.argv[1:]
OUT = _args[_args.index('--json') + 1] if '--json' in _args else None
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html')

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

KIND_META = {'news': ('News', False), 'tool': ('Tool', True), 'read': ('Read', True),
             'quote': ('Quote', True), 'til': ('TIL', True), 'awe': ('Curiosity', True)}

# ---- tagging ----
TAG_RULES = [  # (compiled regex over haystack, slug, label, facet)
    (r'\b(ai|a\.i|llm|llms|gpt|chatgpt|openai|anthropic|claude|gemini|deepseek|grok|mistral|llama|neural|machine learning|\bml\b|prompt|prompts|diffusion|transformer|\brag\b|inference|fine[- ]?tun|agentic|agent|agents|models?)\b', 'ai', 'AI/ML', 'topic'),
    (r'\bopen[- ]?source\b', 'opensource', 'Open source', 'source'),
    (r'\b(css|html|\bdom\b|frontend|front[- ]end|webgpu|wasm|\bpwa\b|service worker|web ?assembly|browser|webrtc|websocket)\b', 'web', 'Web platform', 'topic'),
    (r'\b(javascript|\bjs\b|node|nodejs|npm|pnpm|react|vue|svelte|typescript|\bts\b|deno|\bbun\b|eslint|vite|webpack)\b', 'javascript', 'JavaScript', 'topic'),
    (r'\b(cli|devtool|dev tools|linter|\blint\b|debugger|debug|terminal|docker|kubernetes|\bk8s\b|ci/cd)\b', 'devtools', 'Dev tools', 'topic'),
    (r'\b(database|databases|\bsql\b|postgres|mysql|mongo|mongodb|redis|\bindex\b|indexes|query)\b', 'data', 'Databases', 'topic'),
    (r'\b(career|jobs?|hiring|hire|interview|resume|salary|workplace|productivity|craft)\b', 'career', 'Career & craft', 'topic'),
    (r'\b(security|vulnerabilit|\bcve\b|exploit|encryption|privacy|password|\bauth\b)\b', 'security', 'Security', 'topic'),
    (r'\blangchain|llm[- ]?ops|vector (db|database)|embeddings?\b', 'llm-ops', 'LLM tooling', 'topic'),
]
TAG_RULES = [(re.compile(rx, re.I), s, l, f) for rx, s, l, f in TAG_RULES]
TAG_META = {}
for _, s, l, f in TAG_RULES: TAG_META[s] = (l, f)
for k, (l, _t) in KIND_META.items(): TAG_META[k] = (l, 'kind')
TAG_META['wikipedia'] = ('Wikipedia', 'source')
TAG_META['video'] = ('Video', 'source')

GH_RESERVED = {'apps', 'sponsors', 'marketplace', 'settings', 'about', 'features', 'topics',
               'collections', 'notifications', 'orgs', 'users', 'login', 'join', 'pricing',
               'site', 'security', 'explore', 'trending', 'new', 'organizations', 'dashboard',
               'stars', 'issues', 'pulls', 'codespaces', 'readme', 'search'}
FURNITURE = re.compile(r'(^|\.)(wow|a|p|latest|why|files)\.nibbles\.dev$|nibbles\.dev/(subscribe|survey)|notebooklm\.google\.com|/subscribe|/survey', re.I)

def clean_text(frag):
    return html.unescape(re.sub(r'<[^>]+>', '', frag)).replace('‍', '').strip()

EMOJI = re.compile(r'^(?:[^\w\s,.:;"\'-]|️|‍|\U0001F3FB-\U0001F3FF)+')

def marker_of(text):
    m = re.match(r'^\s*([\U0001F000-\U0001FAFF←-⯿☀-➿️‍]+)', text)
    return m.group(1).strip('‍️ ') if m else None

def entity(url):
    try: p = urlparse(url)
    except Exception: return None, None
    host = (p.netloc or '').lower().split(':')[0]
    if host.startswith('www.'): host = host[4:]
    repo = None
    if host == 'github.com':
        parts = [x for x in p.path.split('/') if x]
        if len(parts) >= 2 and parts[0].lower() not in GH_RESERVED:
            repo = f"{parts[0]}/{re.sub(r'.git$','',parts[1])}".lower()
    return host, repo

def tags_for(kind, title, desc, host, url, repo):
    tags = [kind]
    hay = f"{title} {desc} {host or ''} {url or ''}".lower()
    for rx, s, l, f in TAG_RULES:
        if rx.search(hay) and s not in tags: tags.append(s)
    if repo and 'opensource' not in tags: tags.append('opensource')
    if host and host.endswith('wikipedia.org') and 'wikipedia' not in tags: tags.append('wikipedia')
    if host and re.search(r'(youtube\.com|youtu\.be|vimeo\.com)$', host) and 'video' not in tags: tags.append('video')
    if re.search(r'\b(video|watch|podcast|youtube)\b', hay) and 'video' not in tags: tags.append('video')
    return tags

# ---- parse one edition ----
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
_LEAD = re.compile(r'^[\s.,;:!?)\]…–—"\'-]+')
_CONN = re.compile(r'^(and|but|so|also|plus|yet)\b[\s,]*', re.I)
def tidy_desc(d):
    d = _CONN.sub('', _LEAD.sub('', d)).strip()
    if d:
        d = d[0].upper() + d[1:]
        if d[-1] not in '.!?)”"':
            d += '.'
    return d

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
            before = li[:am.start()]
            marker = marker_of(clean_text(before) or clean_text(li))
            after = clean_text(li[am.end():])
            desc = tidy_desc(after)
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

# ---- aggregate tags ----
tagagg = {}
for e in entries:
    for t in e['tags']:
        a = tagagg.setdefault(t, {'slug': t, 'count': 0, 'eds': set()})
        a['count'] += 1; a['eds'].add(e['ed'])
tags = []
for t, a in tagagg.items():
    label, facet = TAG_META.get(t, (t.title(), 'topic'))
    eds = sorted(a['eds'])
    tags.append({'slug': t, 'label': label, 'facet': facet, 'count': a['count'],
                 'editions': eds, 'first': eds[0], 'last': eds[-1]})
tags.sort(key=lambda x: (-x['count'], x['slug']))

# entity count: distinct url, or quote title
ents = set()
for e in entries:
    ents.add(e.get('url') or ('quote::' + e['title']))

editions_out = []
for ed in sorted(editions_meta):
    m = editions_meta[ed]
    editions_out.append({'number': m['number'], 'slug': m['slug'], 'title': m['title'],
                         'url': 'https://nibbles.dev/p/' + m['slug'],
                         'publishedAt': m['publishedAt'], 'crossRefs': m['crossRefs'],
                         'entryCount': m['entryCount'], 'contentHash': m['contentHash']})

from datetime import datetime, timezone
out = {'generatedAt': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
       'editions': editions_out, 'entries': entries, 'tags': tags, 'entityCount': len(ents)}
blob_json = json.dumps(out, ensure_ascii=False, separators=(',', ':'))

# the page is self-contained: re-inline the index as its BOOTSTRAP blob
lines = open(PAGE, encoding='utf-8').read().split('\n')
for i, l in enumerate(lines):
    if l.startswith('const BOOTSTRAP ='):
        lines[i] = 'const BOOTSTRAP = ' + blob_json + ';'
        open(PAGE, 'w', encoding='utf-8').write('\n'.join(lines))
        print(f"inlined into {PAGE}", file=sys.stderr)
        break
else:
    raise SystemExit("could not find 'const BOOTSTRAP =' line in index.html")

if OUT:  # optional raw JSON dump
    with open(OUT, 'w') as fh: fh.write(blob_json)
    print(f"wrote {OUT}", file=sys.stderr)

# ---- report ----
def pr(*a): print(*a, file=sys.stderr)
pr(f"\n=== PARSE REPORT ===")
pr(f"editions indexed: {len(editions_out)} (#{editions_out[0]['number']}..#{editions_out[-1]['number']})")
pr(f"entries: {len(entries)}   entities: {len(ents)}   tags: {len(tags)}")
byk = Counter(e['kind'] for e in entries)
pr("by kind:", dict(byk))
pr(f"skipped furniture links: {report['skipped_furniture']}")
_page_bytes = open(PAGE, 'rb').read()
pr(f"index data: {len(blob_json.encode())/1024:.1f} KB  |  index.html: {len(_page_bytes)/1024:.1f} KB "
   f"({len(gzip.compress(_page_bytes))/1024:.1f} KB gzipped)")
pr(f"\nUNKNOWN section-level headings (parked in awe, {len(report['unknown'])} distinct):")
for n, c in report['unknown'].most_common():
    eds = sorted(report['unknown_eds'][n]); pr(f"  {c:3d}x  {n!r:45s} eds {eds}")
low = [e for e in entries if not e.get('url') and e['kind'] != 'quote']
pr(f"\nentries with no url (non-quote): {len(low)}")
