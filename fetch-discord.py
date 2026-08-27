"""Harvest links from the Discord channels -> data/discord.json.

Incremental, and per channel: every run asks Discord only for messages *after*
the newest one already harvested in that channel, so the daily job costs a
couple of API calls per channel even though the histories are years long. Each
channel keeps its own cursor, so adding a channel backfills only that one.

What lands in data/discord.json is the raw harvest and nothing else - message
id, timestamp, author, text, urls. Titles, descriptions, tags and dedupe are all
derived later (enrich-links.py, build-page.py), so a re-derivation never needs
the network and a bad heuristic is never baked into the stored data.

Env:
  DISCORD_TOKEN        the token
  DISCORD_CHANNEL_IDS  comma-separated channel ids (DISCORD_CHANNEL_ID also
                       works for a single one)
  DISCORD_TOKEN_TYPE  'bot' (default) or 'user'. A user token authenticates a
                      person rather than an app: it works against this same
                      endpoint, but self-botting breaks Discord's ToS and the
                      account carries the risk. A bot token is free and scoped.

Usage:
  python3 fetch-discord.py                  # incremental, every channel
  python3 fetch-discord.py --backfill       # walk whole histories, oldest first
  python3 fetch-discord.py --only 123,456   # restrict to these channels
  python3 fetch-discord.py --limit 500      # stop after N new messages per channel
"""
import json, os, re, sys, time, urllib.request, urllib.error
from collections import Counter

from taxonomy import is_furniture

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, 'data', 'discord.json')
API = 'https://discord.com/api/v10'

TOKEN = os.environ.get('DISCORD_TOKEN', '').strip()
CHANNELS = [c.strip() for c in
            (os.environ.get('DISCORD_CHANNEL_IDS') or os.environ.get('DISCORD_CHANNEL_ID') or '')
            .replace('\n', ',').split(',') if c.strip()]
# A bot token is sent as "Bot <token>", a user token bare. They are not
# distinguishable by shape, so the type is declared rather than guessed.
TOKEN_TYPE = os.environ.get('DISCORD_TOKEN_TYPE', 'bot').strip().lower()
AUTH = TOKEN if TOKEN_TYPE == 'user' else 'Bot ' + TOKEN

# bare urls, plus <suppressed> ones; markdown links are pulled out separately so
# the link text can seed a title
URL_RE = re.compile(r'https?://[^\s<>()\[\]"\'`]+[^\s<>()\[\]"\'`.,;:!?]', re.I)
MD_LINK_RE = re.compile(r'\[([^\]]{1,200})\]\((https?://[^\s)]+)\)')


def api(path):
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
            if e.code in (401, 403):
                raise SystemExit(
                    f"Discord refused the token ({e.code}), sent as {TOKEN_TYPE!r}. A bot token "
                    f"needs the bot in the server with 'Read Message History' on this channel; "
                    f"a user token needs DISCORD_TOKEN_TYPE=user.\n{body}")
            if 500 <= e.code < 600:
                time.sleep(2 ** attempt); continue
            raise SystemExit(f"Discord API {e.code} on {path}: {body}")
        except urllib.error.URLError as e:
            if attempt == 5: raise
            time.sleep(2 ** attempt)
    raise SystemExit(f"gave up on {path}")


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
    """The harvest, migrated forward from the single-channel shape if needed."""
    if not os.path.exists(STORE):
        return {'guildId': None, 'channels': {}, 'messages': []}
    st = json.load(open(STORE, encoding='utf-8'))
    if 'channels' not in st:                       # pre-multi-channel harvest
        ch = st.get('channelId')
        st['channels'] = {ch: {'name': None, 'lastMessageId': st.get('lastMessageId')}} if ch else {}
        for m in st.get('messages', []):
            m.setdefault('ch', ch)
        st.pop('channelId', None); st.pop('lastMessageId', None)
    return st


def harvest_channel(cid, store, known, backfill, limit):
    """Walk one channel forward from its own cursor. Returns messages kept."""
    meta = store['channels'].setdefault(cid, {'name': None, 'lastMessageId': None})
    try:
        info = api(f'/channels/{cid}')
        meta['name'] = info.get('name') or meta.get('name')
        store['guildId'] = store.get('guildId') or info.get('guild_id')
    except SystemExit:
        raise
    except Exception:
        pass                                       # a name is a nicety, not a blocker

    label = '#' + (meta.get('name') or cid)
    cursor = None if backfill else meta.get('lastMessageId')
    fetched = kept = 0
    while True:
        q = f'/channels/{cid}/messages?limit=100'
        q += f'&after={cursor}' if cursor else '&after=0'
        batch = api(q)
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
                'author': a.get('global_name') or a.get('username'),
                'authorId': a.get('id'),
                'content': (m.get('content') or '').strip(),
                'urls': [{'url': u, 'text': t} for u, t in urls],
            })
            known.add(m['id']); kept += 1
        print(f"\r  {label}: fetched {fetched}, {kept} with links", end='', file=sys.stderr)
        if limit and kept >= limit:
            break
        if len(batch) < 100:
            break
    # the cursor advances past every message SEEN, not every message kept, or
    # a run of link-free chat would be re-fetched forever
    if cursor:
        meta['lastMessageId'] = max(cursor, meta.get('lastMessageId') or '0', key=int)
    print(f"\r  {label}: fetched {fetched}, {kept} with links", file=sys.stderr)
    return kept


def main():
    if not TOKEN or not CHANNELS:
        raise SystemExit('set DISCORD_TOKEN and DISCORD_CHANNEL_IDS')
    args = sys.argv[1:]
    backfill = '--backfill' in args
    limit = int(args[args.index('--limit') + 1]) if '--limit' in args else None
    only = set(args[args.index('--only') + 1].split(',')) if '--only' in args else None

    store = load()
    known = {m['id'] for m in store['messages']}
    todo = [c for c in CHANNELS if not only or c in only]
    if not todo:
        raise SystemExit(f'--only matched none of {CHANNELS}')

    total = 0
    for cid in todo:
        total += harvest_channel(cid, store, known, backfill, limit)

    store['messages'].sort(key=lambda m: int(m['id']))
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    with open(STORE, 'w', encoding='utf-8') as fh:
        json.dump(store, fh, ensure_ascii=False, indent=1)

    nlinks = sum(len(m['urls']) for m in store['messages'])
    per = Counter(m.get('ch') for m in store['messages'])
    print(f"\n=== DISCORD HARVEST ===", file=sys.stderr)
    print(f"channels: {len(store['channels'])}   new messages with links: {total}",
          file=sys.stderr)
    for cid, meta in store['channels'].items():
        print(f"  #{meta.get('name') or cid:<24} {per.get(cid, 0):5d} messages held",
              file=sys.stderr)
    print(f"stored: {len(store['messages'])} messages, {nlinks} links", file=sys.stderr)
    print(f"wrote {STORE} ({os.path.getsize(STORE)/1024:.1f} KB)", file=sys.stderr)
    print(f"\nnext: python3 enrich-links.py && python3 build-page.py", file=sys.stderr)


if __name__ == '__main__':
    main()
