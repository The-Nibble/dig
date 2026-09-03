"""Harvest links from the Discord channels -> data/discord.json.

Point it at a server and it finds the channels itself: every text and
announcement channel @everyone can see, plus the threads under them. A channel
created next month starts being indexed the day it appears, with no config
change and no list of ids to maintain.

Incremental, and per channel: every run asks Discord only for messages *after*
the newest one already harvested there, so the daily job costs a couple of API
calls per channel even though the histories are years long. Each channel keeps
its own cursor, so a newly discovered channel backfills only itself.

What lands in data/discord.json is the raw harvest and nothing else - message
id, timestamp, author, text, urls. Titles, descriptions, tags and dedupe are all
derived later (enrich-links.py, build-page.py), so a re-derivation never needs
the network and a bad heuristic is never baked into the stored data.

Env:
  DISCORD_TOKEN             the token
  DISCORD_GUILD_ID          server id - harvest every public channel in it
  DISCORD_CHANNEL_IDS       or name channels explicitly, comma-separated
                            (DISCORD_CHANNEL_ID also works for a single one)
  DISCORD_EXCLUDE_CHANNELS  channel ids discovery should skip
  DISCORD_TOKEN_TYPE        'bot' (default) or 'user'. A user token authenticates
                            a person rather than an app: it works against these
                            same endpoints, but self-botting breaks Discord's ToS
                            and the account carries the risk. A bot token is free
                            and scoped.

Usage:
  python3 fetch-discord.py                  # incremental, every channel
  python3 fetch-discord.py --list           # show what discovery finds, fetch nothing
  python3 fetch-discord.py --backfill       # whole histories, archived threads too
  python3 fetch-discord.py --only 123,456   # restrict to these channels
  python3 fetch-discord.py --limit 500      # stop after N new messages per channel
"""
import json, os, re, sys, time, urllib.parse, urllib.request, urllib.error
from collections import Counter

from taxonomy import is_furniture

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, 'data', 'discord.json')
API = 'https://discord.com/api/v10'

TOKEN = os.environ.get('DISCORD_TOKEN', '').strip()
GUILD = os.environ.get('DISCORD_GUILD_ID', '').strip()


def _ids(*names):
    raw = next((os.environ.get(n) for n in names if os.environ.get(n)), '')
    return [c.strip() for c in raw.replace('\n', ',').split(',') if c.strip()]


CHANNELS = _ids('DISCORD_CHANNEL_IDS', 'DISCORD_CHANNEL_ID')
EXCLUDE = set(_ids('DISCORD_EXCLUDE_CHANNELS'))
# A bot token is sent as "Bot <token>", a user token bare. They are not
# distinguishable by shape, so the type is declared rather than guessed.
TOKEN_TYPE = os.environ.get('DISCORD_TOKEN_TYPE', 'bot').strip().lower()
AUTH = TOKEN if TOKEN_TYPE == 'user' else 'Bot ' + TOKEN

VIEW_CHANNEL = 1 << 10
TEXTUAL = {0, 5}        # GUILD_TEXT, GUILD_ANNOUNCEMENT - hold messages directly
FORUMS = {15, 16}       # GUILD_FORUM, GUILD_MEDIA - hold nothing but threads
THREADS = {10, 11}      # announcement + public threads; private ones stay private

# bare urls, plus <suppressed> ones; markdown links are pulled out separately so
# the link text can seed a title
URL_RE = re.compile(r'https?://[^\s<>()\[\]"\'`]+[^\s<>()\[\]"\'`.,;:!?]', re.I)
MD_LINK_RE = re.compile(r'\[([^\]]{1,200})\]\((https?://[^\s)]+)\)')


class Denied(Exception):
    """Readable by someone, but not by this token. Skip it, do not abort."""


def api(path, soft=False):
    req = urllib.request.Request(API + path, headers={
        'Authorization': AUTH,
        'User-Agent': 'dig-nibble-indexer (+https://dig.nibbles.dev)',
    })
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'replace')
            if e.code == 429:  # Discord tells us exactly how long to wait
                wait = 1.0
                try: wait = float(json.loads(body).get('retry_after', 1.0))
                except Exception: pass
                print(f"  rate limited, sleeping {wait:.1f}s", file=sys.stderr)
                time.sleep(wait + 0.25); continue
            if e.code == 401:
                raise SystemExit(
                    f"Discord rejected the token outright, sent as {TOKEN_TYPE!r}. "
                    f"A personal token needs DISCORD_TOKEN_TYPE=user.\n{body}")
            if e.code == 403:
                # one locked channel must not take the whole run down with it
                if soft: raise Denied(body)
                raise SystemExit(
                    f"Discord refused access on {path}. A bot needs to be in the server "
                    f"with 'View Channel' and 'Read Message History' here.\n{body}")
            if 500 <= e.code < 600:
                time.sleep(2 ** attempt); continue
            raise SystemExit(f"Discord API {e.code} on {path}: {body}")
        except urllib.error.URLError as e:
            if attempt == 5: raise
            time.sleep(2 ** attempt)
    raise SystemExit(f"gave up on {path}")


# ---- discovery ------------------------------------------------------------
def visible(ch, gid):
    """False when @everyone is denied View Channel - i.e. not a public channel.

    The @everyone role's id is the guild's own id. Discord resolves permissions
    from the channel's own overwrites, so this is the whole check; a category is
    consulted too, since a public channel inside a private category is not one.
    """
    for ov in ch.get('permission_overwrites') or []:
        if str(ov.get('id')) == gid and int(ov.get('deny') or 0) & VIEW_CHANNEL:
            return False
    return True


def archived(cid, since=None, known=()):
    """Return archived threads newer than a saved archive timestamp.

    A thread can be created and auto-archived between daily runs, so parents
    must be checked repeatedly. Pages are newest-first; once a saved timestamp
    is reached, everything older was covered by an earlier successful scan.
    """
    before, out = None, []
    newest = since
    while True:
        q = f'/channels/{cid}/threads/archived/public?limit=100'
        if before:
            q += '&before=' + urllib.parse.quote(before)
        try:
            d = api(q, soft=True)
        except Denied:
            return out, None
        batch = d.get('threads') or []
        stamps = [(th.get('thread_metadata') or {}).get('archive_timestamp')
                  for th in batch]
        if stamps:
            newest = max([stamp for stamp in stamps if stamp] + ([newest] if newest else []))
        out += [th for th, stamp in zip(batch, stamps)
                if not since or not stamp or stamp > since
                or (stamp == since and th['id'] not in known)]
        if since and any(stamp and stamp <= since for stamp in stamps):
            return out, newest
        before = (batch[-1].get('thread_metadata') or {}).get('archive_timestamp') if batch else None
        if not d.get('has_more') or not before:
            return out, newest


def discover(gid, store, backfill):
    """Every public, readable place plus archive cursors to commit on success."""
    chans = api(f'/guilds/{gid}/channels')
    by_id = {c['id']: c for c in chans}
    parents, found = {}, []
    for c in chans:
        if c.get('type') not in TEXTUAL | FORUMS or c['id'] in EXCLUDE:
            continue
        if not visible(c, gid):
            continue
        cat = by_id.get(c.get('parent_id') or '')
        if cat and not visible(cat, gid):
            continue
        parents[c['id']] = c.get('name')
        if c.get('type') in TEXTUAL:
            found.append({'id': c['id'], 'name': c.get('name'), 'type': c['type'],
                          'parent': None})

    # Threads carry real conversation, and a forum channel is nothing but
    # threads, so skipping them would silently drop whole channels.
    seen = set()
    for th in (api(f'/guilds/{gid}/threads/active').get('threads') or []):
        if th.get('type') in THREADS and th.get('parent_id') in parents:
            seen.add(th['id'])
            found.append({'id': th['id'], 'name': th.get('name'), 'type': th['type'],
                          'parent': parents[th['parent_id']]})

    cursors = store.setdefault('archiveCursors', {})
    archive_updates = {}
    for cid, pname in parents.items():
        since = None if backfill else cursors.get(cid)
        threads, newest = archived(cid, since, store.get('channels', {}))
        if newest:
            archive_updates[cid] = newest
        for th in threads:
            if th['id'] in seen:
                continue
            seen.add(th['id'])
            found.append({'id': th['id'], 'name': th.get('name'),
                          'type': th.get('type'), 'parent': pname,
                          'archiveParent': cid})
    return found, archive_updates


# ---- harvest --------------------------------------------------------------
COMMENTARY_CAP = 300


def commentary(content):
    """What someone said around the link - all the page ever uses.

    The raw message is deliberately not kept. This file is committed to a public
    repo, and publishing a community's chat verbatim is a different thing from
    indexing the links in it.
    """
    said = re.sub(r'https?://\S+', ' ', content or '')
    return re.sub(r'\s+', ' ', said).strip(' -\u2013\u2014:\u2022|')[:COMMENTARY_CAP]


def extract(content):
    """[(url, link_text_or_None)] in document order, furniture dropped."""
    out, seen = [], set()
    hint = {}
    for m in MD_LINK_RE.finditer(content or ''):
        hint[m.group(2)] = m.group(1).strip()
    for m in URL_RE.finditer(content or ''):
        url = m.group(0).rstrip('>')
        if is_furniture(url) or url in seen:
            continue
        seen.add(url)
        out.append((url, hint.get(url)))
    return out


def load():
    """The harvest, migrated forward from older shapes if needed."""
    if not os.path.exists(STORE):
        return {'guildId': None, 'channels': {}, 'archiveCursors': {}, 'messages': []}
    st = json.load(open(STORE, encoding='utf-8'))
    if 'channels' not in st:                       # pre-multi-channel harvest
        ch = st.get('channelId')
        st['channels'] = {ch: {'name': None, 'lastMessageId': st.get('lastMessageId')}} if ch else {}
        for m in st.get('messages', []):
            m.setdefault('ch', ch)
        st.pop('channelId', None); st.pop('lastMessageId', None)
    st.setdefault('archiveCursors', {})
    return st


def save(store):
    """Written after every channel, not once at the end: a first backfill walks
    a hundred histories, and losing all of it to one interruption is not a
    reasonable thing to risk when the cursors are already correct."""
    store['messages'].sort(key=lambda m: int(m['id']))
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(store, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, STORE)          # never leave a half-written harvest behind


def harvest_channel(info, store, known, backfill, limit):
    """Walk one channel or thread forward from its own cursor. Returns kept."""
    cid = info['id']
    meta = store['channels'].setdefault(cid, {'name': None, 'lastMessageId': None})
    if info.get('name'):
        meta['name'] = info['name']
        if info.get('parent'):
            meta['parent'] = info['parent']
    else:
        # named explicitly rather than discovered: ask who it is
        try:
            d = api(f'/channels/{cid}')
            meta['name'] = d.get('name') or meta.get('name')
            store['guildId'] = store.get('guildId') or d.get('guild_id')
        except SystemExit:
            raise
        except Exception:
            pass                                   # a name is a nicety, not a blocker

    label = ('#' + (meta.get('parent') + ' > ' if meta.get('parent') else '')
             + (meta.get('name') or cid))
    cursor = None if backfill else meta.get('lastMessageId')
    fetched = kept = 0
    try:
        while True:
            q = f'/channels/{cid}/messages?limit=100'
            q += f'&after={cursor}' if cursor else '&after=0'
            batch = api(q, soft=True)
            if not batch:
                break
            batch.sort(key=lambda m: int(m['id']))     # Discord returns newest-first
            fetched += len(batch)
            for m in batch:
                cursor = m['id']
                if m['id'] in known:
                    continue
                urls = extract(m.get('content', ''))
                # a link posted as an embed-only message still counts
                for emb in m.get('embeds') or []:
                    u = emb.get('url')
                    if u and u not in [x[0] for x in urls] and not is_furniture(u):
                        urls.append((u, (emb.get('title') or '').strip() or None))
                if not urls:
                    continue
                a = m.get('author') or {}
                store['messages'].append({
                    'id': m['id'], 'ch': cid,
                    'ts': m.get('timestamp'),
                    # a display name is enough to credit a share; the account
                    # id is never read downstream and this file is committed
                    'author': a.get('global_name') or a.get('username'),
                    'said': commentary(m.get('content')),
                    'urls': [{'url': u, 'text': t} for u, t in urls],
                })
                known.add(m['id']); kept += 1
            print(f"\r  {label}: fetched {fetched}, {kept} with links", end='', file=sys.stderr)
            if limit and kept >= limit:
                break
            if len(batch) < 100:
                break
    except Denied:
        print(f"  {label}: no read access, skipped", file=sys.stderr)
        meta['denied'] = True
        return None
    meta.pop('denied', None)
    # the cursor advances past every message SEEN, not every message kept, or
    # a run of link-free chat would be re-fetched forever
    if cursor:
        meta['lastMessageId'] = max(cursor, meta.get('lastMessageId') or '0', key=int)
    print(f"\r  {label}: fetched {fetched}, {kept} with links", file=sys.stderr)
    return kept


def main():
    args = sys.argv[1:]
    if not TOKEN:
        raise SystemExit('set DISCORD_TOKEN')
    if not GUILD and not CHANNELS:
        raise SystemExit('set DISCORD_GUILD_ID (every public channel in the server) '
                         'or DISCORD_CHANNEL_IDS (an explicit list)')
    backfill = '--backfill' in args
    limit = int(args[args.index('--limit') + 1]) if '--limit' in args else None
    only = set(args[args.index('--only') + 1].split(',')) if '--only' in args else None

    store = load()
    known = {m['id'] for m in store['messages']}

    if GUILD:
        store['guildId'] = store.get('guildId') or GUILD
        targets, archive_updates = discover(GUILD, store, backfill)
        print(f"discovered {len(targets)} public channels and threads", file=sys.stderr)
    else:
        targets = [{'id': c} for c in CHANNELS]
        archive_updates = {}
    skipped_archive_parents = {
        t['archiveParent'] for t in targets
        if t.get('archiveParent') and
        (t['id'] in EXCLUDE or (only and t['id'] not in only))
    }
    targets = [t for t in targets if t['id'] not in EXCLUDE and (not only or t['id'] in only)]
    if not targets:
        raise SystemExit('nothing to harvest - check --only, DISCORD_EXCLUDE_CHANNELS, '
                         'and that the token can see the server')

    if '--list' in args:
        for t in targets:
            where = f"#{t['parent']} > " if t.get('parent') else '#'
            print(f"  {t['id']}  {where}{t.get('name') or '?'}")
        return

    total = 0
    failed_archive_parents = skipped_archive_parents
    for n, t in enumerate(targets, 1):
        print(f"[{n}/{len(targets)}]", end=' ', file=sys.stderr)
        try:
            kept = harvest_channel(t, store, known, backfill, limit)
            if kept is None:
                if t.get('archiveParent'):
                    failed_archive_parents.add(t['archiveParent'])
            else:
                total += kept
        except KeyboardInterrupt:
            save(store)
            raise SystemExit(f"\ninterrupted - kept {total} new messages, "
                             f"rerun to carry on from here")
        save(store)

    for cid, cursor in archive_updates.items():
        if cid not in failed_archive_parents:
            store['archiveCursors'][cid] = cursor
    # Migration from the one-shot scheme is complete only after the recurring
    # scan finishes; an interrupted run safely repeats work using message IDs.
    store.pop('archivedScanned', None)
    save(store)

    nlinks = sum(len(m['urls']) for m in store['messages'])
    per = Counter(m.get('ch') for m in store['messages'])
    print(f"\n=== DISCORD HARVEST ===", file=sys.stderr)
    print(f"places read: {len(targets)}   new messages with links: {total}", file=sys.stderr)
    held = Counter()
    for cid, meta in store['channels'].items():
        held[meta.get('parent') or meta.get('name') or cid] += per.get(cid, 0)
    for name, n in held.most_common():
        print(f"  #{name:<24} {n:5d} messages held", file=sys.stderr)
    denied = [m.get('name') or c for c, m in store['channels'].items() if m.get('denied')]
    if denied:
        print(f"skipped, no read access: {', '.join(denied)}", file=sys.stderr)
    print(f"stored: {len(store['messages'])} messages, {nlinks} links", file=sys.stderr)
    print(f"wrote {STORE} ({os.path.getsize(STORE)/1024:.1f} KB)", file=sys.stderr)
    print(f"\nnext: python3 enrich-links.py && python3 build-page.py", file=sys.stderr)


if __name__ == '__main__':
    main()
