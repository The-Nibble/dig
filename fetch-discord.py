"""Harvest links from the Discord #nibble channel -> data/discord.json.

Incremental: every run asks Discord only for messages *after* the newest one
already harvested, so the daily job costs a couple of API calls even though the
channel history is years long.

What lands in data/discord.json is the raw harvest and nothing else - message
id, timestamp, author, text, urls. Titles, descriptions, tags and dedupe are all
derived later (enrich-links.py, build-page.py), so a re-derivation never needs
the network and a bad heuristic is never baked into the stored data.

Env:
  DISCORD_TOKEN       bot token (recommended) or user token
  DISCORD_CHANNEL_ID  the #nibble channel id

Usage:
  python3 fetch-discord.py              # incremental: newest -> forward
  python3 fetch-discord.py --backfill   # walk the whole history, oldest first
  python3 fetch-discord.py --limit 500  # stop after N new messages (testing)
"""
import json, os, re, sys, time, urllib.request, urllib.error

from taxonomy import is_furniture

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, 'data', 'discord.json')
API = 'https://discord.com/api/v10'

TOKEN = os.environ.get('DISCORD_TOKEN', '').strip()
CHANNEL = os.environ.get('DISCORD_CHANNEL_ID', '').strip()

# bare urls, plus <suppressed> ones; markdown links are pulled out separately so
# the link text can seed a title
URL_RE = re.compile(r'https?://[^\s<>()\[\]"\'`]+[^\s<>()\[\]"\'`.,;:!?]', re.I)
MD_LINK_RE = re.compile(r'\[([^\]]{1,200})\]\((https?://[^\s)]+)\)')


def api(path):
    req = urllib.request.Request(API + path, headers={
        'Authorization': TOKEN if TOKEN.lower().startswith('bot ') else 'Bot ' + TOKEN,
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
                    f"Discord refused the token ({e.code}). A bot token needs the bot to be in "
                    f"the server with 'Read Message History' on this channel.\n{body}")
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
    if os.path.exists(STORE):
        return json.load(open(STORE, encoding='utf-8'))
    return {'channelId': CHANNEL, 'lastMessageId': None, 'messages': []}


def main():
    if not TOKEN or not CHANNEL:
        raise SystemExit('set DISCORD_TOKEN and DISCORD_CHANNEL_ID')
    args = sys.argv[1:]
    backfill = '--backfill' in args
    limit = int(args[args.index('--limit') + 1]) if '--limit' in args else None

    store = load()
    store['channelId'] = CHANNEL
    known = {m['id'] for m in store['messages']}

    # 'after' walks forward from the newest we hold; a backfill starts from zero
    # and walks the entire history the same way, so both paths share this loop.
    cursor = None if backfill else store.get('lastMessageId')
    fetched = kept = 0
    while True:
        q = f'/channels/{CHANNEL}/messages?limit=100'
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
                'id': m['id'],
                'ts': m.get('timestamp'),
                'author': a.get('global_name') or a.get('username'),
                'authorId': a.get('id'),
                'content': (m.get('content') or '').strip(),
                'urls': [{'url': u, 'text': t} for u, t in urls],
            })
            known.add(m['id']); kept += 1
        print(f"\r  fetched {fetched} messages, {kept} with links", end='', file=sys.stderr)
        if limit and kept >= limit:
            break
        if len(batch) < 100:
            break
    print(file=sys.stderr)

    store['messages'].sort(key=lambda m: int(m['id']))
    if store['messages']:
        store['lastMessageId'] = store['messages'][-1]['id']
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    with open(STORE, 'w', encoding='utf-8') as fh:
        json.dump(store, fh, ensure_ascii=False, indent=1)

    nlinks = sum(len(m['urls']) for m in store['messages'])
    print(f"\n=== DISCORD HARVEST ===", file=sys.stderr)
    print(f"new messages with links this run: {kept}", file=sys.stderr)
    print(f"stored: {len(store['messages'])} messages, {nlinks} links", file=sys.stderr)
    print(f"wrote {STORE} ({os.path.getsize(STORE)/1024:.1f} KB)", file=sys.stderr)
    print(f"\nnext: python3 enrich-links.py && python3 build-page.py", file=sys.stderr)


if __name__ == '__main__':
    main()
