"""Merge every source, dedupe, and inline the result into index.html.

Inputs (all committed, none requiring the Substack export):
  data/nibble.json     <- build-index.py, run by hand when a new edition lands
  data/discord.json    <- fetch-discord.py, run daily
  data/link-meta.json  <- enrich-links.py, titles/blurbs for bare links

This is the only script that writes index.html, and it is pure: same inputs,
same page. That is what lets the daily job rebuild without the archive.

Usage:
  python3 build-page.py
  python3 build-page.py --json out.json   # also dump the merged index
"""
import gzip, json, os, re, sys
from collections import Counter, defaultdict
from urllib.parse import unquote
from datetime import datetime, timezone

from taxonomy import (KIND_META, TAG_META, canonical, entity, is_furniture, is_job,
                      tags_for, tidy_desc)

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, 'index.html')
D = os.path.join(HERE, 'data')


def load(name, default=None):
    p = os.path.join(D, name)
    if not os.path.exists(p):
        return default
    return json.load(open(p, encoding='utf-8'))


# ---- discord harvest -> entries -------------------------------------------
# Article hosts, so a shared blog post is filed as a Read rather than a Tool.
# Everything else defaults to Tool: on this channel a bare link is usually a
# thing you can go and use.
READ_HOST = re.compile(
    r'(^|\.)(medium\.com|substack\.com|dev\.to|hashnode\.(dev|com)|blog\.[\w.-]+|[\w-]+\.blog'
    r'|nytimes\.com|theverge\.com|wired\.com|arstechnica\.com|theatlantic\.com|newyorker\.com'
    r'|economist\.com|ft\.com|bloomberg\.com|quantamagazine\.org|aeon\.co|arxiv\.org)$', re.I)


# A channel whose name says what it is overrides the host heuristic: a link in
# #reads is a read even when it lives on github.io.
CHANNEL_KIND = [(re.compile(r'read|article|blog|longform|paper', re.I), 'read'),
                (re.compile(r'til|learn|today-i', re.I), 'til'),
                (re.compile(r'tool|build|ship|oss|project', re.I), 'tool')]


def kind_for(chan_name, host):
    for rx, k in CHANNEL_KIND:
        if chan_name and rx.search(chan_name):
            return k
    return 'read' if (host and READ_HOST.search(host)) else 'tool'


# Channels that are harvested but deliberately not surfaced. The harvest stays
# lossless, so this is a display decision and reversible without re-fetching:
# drop a name from here and the next build shows it.
HIDE_CHANNELS = {'liked-phrases', 'memes', 'introductions', 'job-posts'}


def shown_channels(harvest):
    out = {}
    for cid, c in (harvest.get('channels') or {}).items():
        name = c.get('parent') or c.get('name')
        if name and name.lstrip('#') in HIDE_CHANNELS:
            continue
        out[cid] = c
    return out


def channel_slugs(channels):
    """Channel name -> tag slug. A channel is a facet you can click, which is
    the only way "everything from #tools" is answerable without a search box.
    Prefixed only on collision, so the chip normally reads exactly '#tools'."""
    out = {}
    for c in channels.values():
        name = (c.get('parent') or c.get('name') or '').strip()
        if not name or name in out:
            continue
        slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
        if slug:
            out[name] = 'ch-' + slug if slug in TAG_META else slug
    return out


def discord_entries(harvest, meta):
    guild = harvest.get('guildId')
    channels = shown_channels(harvest)
    slugs = channel_slugs(channels)
    out = []
    for m in harvest.get('messages', []):
        cid = m.get('ch')
        if cid not in channels:
            continue                    # harvested, but not shown
        cmeta = channels[cid]
        # a forum post is a thread; label it with the channel it sits in, or
        # every post title becomes its own one-row 'channel'
        chan = cmeta.get('parent') or cmeta.get('name')
        date = (m.get('ts') or '')[:10]
        msg_url = (f'https://discord.com/channels/{guild}/{cid}/{m["id"]}'
                   if guild and cid else None)
        # the blurb someone wrote around the link, when there was one. Most
        # messages are a bare link, which is why link-meta exists. Harvests made
        # before the trim stored the whole message, so both shapes are read.
        said = m.get('said')
        if said is None:
            said = re.sub(r'https?://\S+', ' ', m.get('content') or '')
        said = re.sub(r'\s+', ' ', said).strip(' -–—:•|')
        for u in m['urls']:
            url = u['url']
            # a vacancy is not an archive entry; the newsletter's own curated
            # links are left alone, since those were a deliberate choice
            if is_furniture(url) or is_job(url) or not canonical(url):
                continue
            key = canonical(url)
            mm = meta.get(key) or {}
            host, repo = entity(url)
            # a link text or an og:title that is itself a url names nothing;
            # some sites really do put their canonical url in <title>
            title = (u.get('text') or '').strip()
            if not title or re.match(r'^https?://', title):
                title = (mm.get('title') or '').strip()
            if not title or re.match(r'^https?://', title):
                title = name_from_url(url, host, repo)
            # "good thread" is an aside, not a description: a substantial remark
            # beats the fetched blurb, a brief one only stands in when there is
            # no blurb at all, and "Damn." is worse than saying nothing.
            said_desc = tidy_desc(said)
            desc = (said_desc if len(said_desc) >= 40 else '') \
                or (mm.get('description') or '') \
                or (said_desc if len(said_desc) >= 20 else '')
            kind = kind_for(chan, host)
            e = {'src': 'discord', 'kind': kind, 'timeless': True,
                 'heading': KIND_META[kind][0],
                 'title': title[:200], 'description': desc[:500],
                 'url': url, 'date': date, 'msg': m['id']}
            if cid: e['ch'] = cid
            if chan: e['chan'] = chan
            if msg_url: e['msgUrl'] = msg_url
            if m.get('author'): e['by'] = m['author']
            if host: e['domain'] = host
            if repo: e['repo'] = repo
            e['tags'] = tags_for(kind, e['title'], e['description'], host, url, repo) + ['discord']
            if chan and slugs.get(chan):
                e['tags'].append(slugs[chan])
            out.append(e)
    return out


# Hosts whose last path segment is an opaque id, so reading the url produces
# noise rather than a name: archive.is/xArCk, imdb.com/title/tt5537002.
OPAQUE = re.compile(r'(^|\.)(archive\.(is|ph|today|vn|li)|imdb\.com)$', re.I)


def name_from_url(url, host, repo):
    """Last resort when nothing named the link: read the url itself."""
    if repo: return repo.split('/')[-1]
    if host and OPAQUE.search(host):
        return host
    seg = [s for s in re.sub(r'^https?://[^/]+', '', url).split('?')[0].split('/') if s]
    if seg:
        # a wiki slug arrives percent-encoded; 'Cunningham%27s Law' is not a title
        s = unquote(re.sub(r'\.(html?|php|aspx?|md|pdf)$', '', seg[-1]))
        s = re.sub(r'[-_+]+', ' ', s).strip()
        if s and not re.fullmatch(r'[\d\W]+', s) and len(s) > 2:
            return s[:1].upper() + s[1:]
    return host or url


# ---- dedupe ---------------------------------------------------------------
# The same link shows up several times: twice in the newsletter years apart,
# in Discord and then in an edition, or three times in Discord in one week.
# One entry survives; every other sighting is folded into `also`, which is what
# makes "when did we first talk about X" answerable across sources.
def occurrence(e):
    o = {'src': e['src'], 'date': e.get('date')}
    if e.get('ed'): o['ed'] = e['ed']
    if e.get('chan'): o['chan'] = e['chan']
    if e.get('msgUrl'): o['msgUrl'] = e['msgUrl']
    if e.get('by'): o['by'] = e['by']
    return o


def richness(e):
    """Which sighting should be the one on the page."""
    return (
        1 if e['src'] == 'nibble' else 0,          # curated copy wins outright
        len(e.get('descriptionClean') or e.get('description') or ''),
        1 if e.get('title') and not e['title'].startswith('http') else 0,
    )


def dedupe(entries):
    groups = defaultdict(list)
    singles = []
    for e in entries:
        k = canonical(e['url']) if e.get('url') else None
        if k: groups[k].append(e)
        else: singles.append(e)      # quotes and other url-less entries

    merged = []
    collapsed = 0
    for k, g in groups.items():
        g.sort(key=lambda e: (e.get('date') or '', e.get('id') or 0))
        best = max(g, key=richness)
        keep = dict(best)
        if len(g) > 1:
            collapsed += len(g) - 1
            keep['also'] = [occurrence(e) for e in g if e is not best]
            # tags are a union: a Discord sighting still earns the discord chip
            tags = list(keep['tags'])
            for e in g:
                for t in e['tags']:
                    if t not in tags: tags.append(t)
            keep['tags'] = tags
            # the honest first-seen date is the earliest sighting anywhere
            keep['first'] = min(e['date'] for e in g if e.get('date'))
            keep['last'] = max(e['date'] for e in g if e.get('date'))
        merged.append(keep)
    return merged + singles, collapsed


def main():
    nib = load('nibble.json')
    if not nib:
        raise SystemExit('no data/nibble.json - run build-index.py first')
    harvest = load('discord.json', {'messages': []})
    meta = load('link-meta.json', {})

    entries = list(nib['entries'])
    dis = discord_entries(harvest, meta)
    n_nib, n_dis = len(entries), len(dis)
    entries, collapsed = dedupe(entries + dis)

    # nibble first in original order, then discord by message id: new links
    # always land at the end, so vectors.f32 only ever grows at the tail
    entries.sort(key=lambda e: (0, e.get('id') or 0) if e['src'] == 'nibble'
                 else (1, int(e.get('msg') or 0)))
    for i, e in enumerate(entries, 1):
        e['id'] = i
        e.setdefault('first', e.get('date'))
        e.setdefault('last', e.get('date'))

    # tag counts only; first/last is computed in the page from the live hit set,
    # so a chip and a free-text search can never disagree about a span
    agg = Counter(t for e in entries for t in e['tags'])
    meta_of = dict(TAG_META)
    for name, slug in channel_slugs(shown_channels(harvest)).items():
        meta_of.setdefault(slug, ('#' + name, 'channel'))
    tags = [{'slug': t, 'label': meta_of.get(t, (t.title(), 'topic'))[0],
             'facet': meta_of.get(t, (t.title(), 'topic'))[1], 'count': c}
            for t, c in agg.items()]
    tags.sort(key=lambda x: (-x['count'], x['slug']))

    out = {'generatedAt': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
           'editions': nib['editions'], 'entries': entries, 'tags': tags,
           'sources': {'nibble': sum(1 for e in entries if e['src'] == 'nibble'),
                       'discord': sum(1 for e in entries if e['src'] == 'discord'),
                       # from the harvest, not the surviving rows: a channel
                       # whose links all deduped into newsletter entries was
                       # still a channel we read
                       'channels': sorted({c.get('parent') or c.get('name')
                                           for c in shown_channels(harvest).values()
                                           if c.get('parent') or c.get('name')})},
           'entityCount': len({e.get('url') and canonical(e['url']) or ('quote::' + e['title'])
                               for e in entries})}
    lines = open(PAGE, encoding='utf-8').read().split('\n')

    # This script is meant to be pure - same inputs, same page - but a fresh
    # timestamp on every run broke that: index.html always differed, so the
    # daily job committed even on days when nothing arrived. Keep the old stamp
    # when nothing else moved, which also makes "last updated" mean the last
    # time the index actually changed rather than the last time CI woke up.
    for l in lines:
        if l.startswith('const BOOTSTRAP ='):
            try:
                prev = json.loads(l[len('const BOOTSTRAP ='):].strip().rstrip(';'))
                if {k: v for k, v in prev.items() if k != 'generatedAt'} == \
                   {k: v for k, v in out.items() if k != 'generatedAt'}:
                    out['generatedAt'] = prev['generatedAt']
            except Exception:
                pass                      # unreadable previous build: just restamp
            break

    blob = json.dumps(out, ensure_ascii=False, separators=(',', ':'))
    for i, l in enumerate(lines):
        if l.startswith('const BOOTSTRAP ='):
            lines[i] = 'const BOOTSTRAP = ' + blob + ';'
            open(PAGE, 'w', encoding='utf-8').write('\n'.join(lines))
            break
    else:
        raise SystemExit("could not find 'const BOOTSTRAP =' line in index.html")

    if '--json' in sys.argv:
        p = sys.argv[sys.argv.index('--json') + 1]
        open(p, 'w', encoding='utf-8').write(blob)

    def pr(*a): print(*a, file=sys.stderr)
    page_bytes = open(PAGE, 'rb').read()
    pr("\n=== MERGE REPORT ===")
    if harvest.get('fixture'):
        pr("! data/discord.json is SYNTHETIC (make-fixture.py). Do not commit this page.")
    pr(f"in:  nibble {n_nib} + discord {n_dis} = {n_nib + n_dis}")
    pr(f"out: {len(entries)} entries  ({collapsed} duplicate sightings folded in)")
    pr(f"     {out['sources']['nibble']} shown from the newsletter, "
       f"{out['sources']['discord']} Discord-only")
    per = Counter(e.get('chan') or e.get('ch') for e in entries if e['src'] == 'discord')
    for chan, n in per.most_common():
        pr(f"       #{chan or '?':<22} {n:5d} shown")
    both = [e for e in entries if e.get('also') and
            {o['src'] for o in e['also']} | {e['src']} == {'nibble', 'discord'}]
    pr(f"     {len(both)} links seen in BOTH the newsletter and Discord")
    early = [e for e in both if e['first'] < (e.get('date') or '')]
    pr(f"     {len(early)} of those surfaced in Discord before the edition ran")
    pr(f"by kind: {dict(Counter(e['kind'] for e in entries))}")
    pr(f"tags: {len(tags)}   entities: {out['entityCount']}")
    pr(f"index data: {len(blob.encode())/1024:.1f} KB  |  index.html: {len(page_bytes)/1024:.1f} KB "
       f"({len(gzip.compress(page_bytes))/1024:.1f} KB gzipped)")
    vec = os.path.join(HERE, 'vectors.f32')
    if os.path.exists(vec):
        rows = os.path.getsize(vec) // (384 * 4)
        if rows != len(entries):
            pr(f"\n! vectors.f32 holds {rows} rows, index now has {len(entries)}."
               f"\n  The page falls back to embedding in-browser until you run build-vectors.py.")


if __name__ == '__main__':
    main()
