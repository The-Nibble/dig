"""Synthesise a data/discord.json so the pipeline can be tested without a token.

Builds a harvest that exercises the cases that actually break things: the same
link arriving in Discord and in the newsletter, a youtu.be short link against a
full watch url, tracking params, the same link in two different channels, chat
furniture that must be dropped, messages with and without a human blurb, and
channel names that should override the kind heuristic.

    python3 make-fixture.py && python3 enrich-links.py && python3 build-page.py

Overwrites data/discord.json. Never run this over a real harvest.
"""
import json, os, random, re, sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, 'data', 'discord.json')

if os.path.exists(STORE):
    cur = json.load(open(STORE, encoding='utf-8'))
    if not cur.get('fixture') and cur.get('messages'):
        raise SystemExit(f'{STORE} holds a real harvest ({len(cur["messages"])} messages). '
                         f'Delete it first if you really mean to replace it.')

nib_path = os.path.join(HERE, 'data', 'nibble.json')
if not os.path.exists(nib_path):
    raise SystemExit('run build-index.py first - the fixture reuses real newsletter links')
entries = [e for e in json.load(open(nib_path, encoding='utf-8'))['entries']
           if e.get('url', '').startswith('http')]

rng = random.Random(11)
people = ['aashutosh', 'kunal', 'shreya', 'devansh']
CHANNELS = {'900000000000000002': 'nibble',
            '900000000000000003': 'tools',
            '900000000000000004': 'reads'}
CIDS = list(CHANNELS)
msgs, mid = [], 1050000000000000000


def add(url, text, when, author, cid=None):
    global mid
    mid += rng.randint(10**6, 10**9)
    msgs.append({'id': str(mid), 'ch': cid or rng.choice(CIDS),
                 'ts': when + 'T09:14:00.000000+00:00', 'author': author,
                 'said': text, 'urls': [{'url': url, 'text': None}]})


# 20 links that also ran in the newsletter, shared in Discord weeks earlier
for e in rng.sample(entries, 20):
    url = e['url']
    m = re.match(r'https?://(?:www\.)?youtube\.com/watch\?v=([\w-]+)', url)
    if m and rng.random() < 0.6:
        url = 'https://youtu.be/' + m.group(1)          # short-link canonicalisation
    url += ('&' if '?' in url else '?') + 'utm_source=discord&utm_medium=share'
    y, mo, d = map(int, e['date'].split('-'))
    when = (date(y, mo, d) - timedelta(days=rng.randint(3, 160))).isoformat()
    add(url, rng.choice(['', '', 'this is neat', f'{e["title"]} — worth a look']),
        when, rng.choice(people))

# links only ever seen in the channel
only = [
    ('https://github.com/astral-sh/uv', 'crazy fast pip replacement, written in rust'),
    ('https://github.com/astral-sh/uv', ''),                    # same link, another channel
    ('https://zed.dev/', ''),
    ('https://bun.sh/', 'bun 1.2 is out'),
    ('https://www.youtube.com/watch?v=dQw4w9WgXcQ', ''),
    ('https://youtu.be/dQw4w9WgXcQ?si=abc123', 'reposting this'),  # same video, two forms
    ('https://sqlite.org/fasterthanfs.html', 'sqlite is faster than the filesystem, still wild'),
    ('https://en.wikipedia.org/wiki/Cunningham%27s_Law', ''),
    ('https://news.ycombinator.com/item?id=39215786', 'good thread'),
    ('https://news.ycombinator.com/item?id=39100000', 'different thread'),
    ('https://ghostty.org/', ''),
    ('https://www.anthropic.com/engineering/building-effective-agents', ''),
    ('https://tenor.com/view/lol-gif-123', 'lol'),              # furniture, must be dropped
    ('https://discord.com/channels/1/2/3', 'see above'),        # furniture, must be dropped
]
for url, text in only:
    add(url, text, f'2026-0{rng.randint(1, 8)}-{rng.randint(10, 28)}', rng.choice(people))

# a link in #reads must be filed as a Read even though it is a github.io host
add('https://simonwillison.net/2024/Dec/31/llms-in-2024/', 'best writeup of the year',
    '2026-02-14', 'shreya', cid='900000000000000004')

msgs.sort(key=lambda m: int(m['id']))
os.makedirs(os.path.dirname(STORE), exist_ok=True)
json.dump({'fixture': True, 'guildId': '900000000000000001',
           'channels': {cid: {'name': n, 'lastMessageId': msgs[-1]['id']}
                        for cid, n in CHANNELS.items()},
           'messages': msgs},
          open(STORE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print(f"wrote {STORE}: {len(msgs)} synthetic messages across {len(CHANNELS)} channels, "
      f"{sum(len(m['urls']) for m in msgs)} links", file=sys.stderr)
print("next: python3 enrich-links.py && python3 build-page.py", file=sys.stderr)
