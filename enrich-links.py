"""Give every harvested link a title and a blurb -> data/link-meta.json.

A Nibble entry arrives already named ("[Title](url) - what it does"). A Discord
message is usually a bare url with nothing around it, so without this step half
the index would be titled "github.com/foo/bar". This fetches that missing text.

Cached by canonical url and committed, so the daily job only ever touches links
it has never seen. Failures are cached too, with a retry-after stamp, so a site
that blocks us is not re-fetched on every run.

Well-known hosts answer through a real API rather than a page scrape - those are
faster, kinder and far more reliable than parsing whatever HTML ships that day.

Usage:
  python3 enrich-links.py             # fill gaps for links in data/discord.json
  python3 enrich-links.py --retry     # also re-attempt previously failed links
  python3 enrich-links.py --refetch 'x[.]com'  # redo links matching a url pattern
  python3 enrich-links.py --prune     # drop cache keys the harvest no longer wants
  python3 enrich-links.py --limit 50  # cap the number of network fetches
  python3 enrich-links.py --gaps g.json    # list links still lacking a title
  python3 enrich-links.py --merge a.json   # fold researched titles/blurbs back in

The last two exist for the links a regex scraper cannot read - bot walls, pages
that render their title in JavaScript. Something that can actually fetch the page
fills those in. Merged records are marked `via` so an agent-sourced blurb is
never mistaken for the page's own metadata, and a plain rerun will not clobber it.
"""
import contextlib, http.client, html as htmllib, ipaddress, json, os, re, signal
import socket, ssl, sys, time
import urllib.parse
import zlib
from datetime import datetime, timezone, timedelta

from taxonomy import canonical, entity, is_furniture, tidy_desc

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, 'data', 'link-meta.json')
HARVEST = os.path.join(HERE, 'data', 'discord.json')

UA = ('Mozilla/5.0 (compatible; dig-nibble-indexer/1.0; +https://dig.nibbles.dev) '
      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36')
# Strings that are never a description of anything - consent walls, login
# prompts, JS warnings. A page that only offers these is better left blank:
# the title still carries the entry, and an empty blurb lets the message text
# stand in.
BOILERPLATE = re.compile(
    r'log ?in or sign ?up|sign in to |create an account|enable javascript'
    r'|subscribe to (continue|read)|we use cookies|are you a robot'
    r'|access denied|just a moment|checking your browser|please verify', re.I)

TIMEOUT = 15
MAX_BYTES = 300_000        # meta tags live in <head>; no need for the whole page
MAX_REDIRECTS = 3
TOTAL_BUDGET_SECONDS = int(os.environ.get('ENRICH_BUDGET_SECONDS', 40 * 60))
RETRY_AFTER_DAYS = 30      # a dead link stays dead; check again next month


class UnsafeDestination(ValueError):
    """A URL resolves somewhere an untrusted Discord link must not reach."""


class FetchError(RuntimeError):
    """A remote response could not be used for enrichment."""


@contextlib.contextmanager
def request_deadline(seconds):
    """Bound the whole request, not only periods of socket inactivity."""
    if not hasattr(signal, 'SIGALRM'):
        yield
        return
    previous = signal.getsignal(signal.SIGALRM)

    def expired(_signum, _frame):
        raise TimeoutError(f'fetch exceeded {seconds}s')

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def public_address(host, port):
    """Resolve once, reject mixed/private answers, and return a pinned address."""
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise FetchError(f'could not resolve {host}: {error}') from error
    addresses = []
    for answer in answers:
        raw = answer[4][0].split('%', 1)[0]
        address = ipaddress.ip_address(raw)
        if not address.is_global:
            raise UnsafeDestination(f'{host} resolves to non-public address {address}')
        if raw not in addresses:
            addresses.append(raw)
    if not addresses:
        raise FetchError(f'{host} resolved without an address')
    return addresses[0]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, address, port, timeout):
        super().__init__(host, port=port, timeout=timeout)
        self._address = address

    def connect(self):
        self.sock = socket.create_connection(
            (self._address, self.port), self.timeout, self.source_address)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, port, timeout):
        super().__init__(host, port=port, timeout=timeout,
                         context=ssl.create_default_context())
        self._address = address

    def connect(self):
        self.sock = socket.create_connection(
            (self._address, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def open_public(url, headers):
    """Open one validated URL without a second, rebindable DNS lookup."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise UnsafeDestination('only absolute http and https URLs are allowed')
    if parsed.username or parsed.password:
        raise UnsafeDestination('credentials in URLs are not allowed')
    try:
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    except ValueError as error:
        raise UnsafeDestination(f'invalid port in {url}') from error
    address = public_address(parsed.hostname, port)
    cls = _PinnedHTTPSConnection if parsed.scheme == 'https' else _PinnedHTTPConnection
    conn = cls(parsed.hostname, address, port, TIMEOUT)
    path = urllib.parse.urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
    conn.request('GET', path, headers=headers)
    return conn, conn.getresponse()


def read_limited(response):
    """Read at most MAX_BYTES after decompression, including gzip bombs."""
    decoder = (zlib.decompressobj(16 + zlib.MAX_WBITS)
               if response.getheader('Content-Encoding', '').lower() == 'gzip' else None)
    out = bytearray()
    wire_bytes = 0
    while len(out) < MAX_BYTES:
        chunk = response.read(min(16_384, MAX_BYTES - wire_bytes + 1))
        if not chunk:
            break
        wire_bytes += len(chunk)
        if wire_bytes > MAX_BYTES:
            raise FetchError(f'response exceeds {MAX_BYTES} compressed bytes')
        remaining = MAX_BYTES - len(out)
        out.extend(decoder.decompress(chunk, remaining) if decoder else chunk[:remaining])
    return bytes(out)


def get(url, accept='text/html,application/xhtml+xml,*/*;q=0.8', headers=None):
    request_headers = {
        'User-Agent': UA, 'Accept': accept,
        'Accept-Language': 'en-US,en;q=0.9', 'Accept-Encoding': 'gzip',
        **(headers or {}),
    }
    with request_deadline(TIMEOUT):
        original_host = urllib.parse.urlsplit(url).hostname
        current = url
        for redirect_count in range(MAX_REDIRECTS + 1):
            redirected_headers = dict(request_headers)
            if urllib.parse.urlsplit(current).hostname != original_host:
                redirected_headers.pop('Authorization', None)
            conn = response = None
            try:
                conn, response = open_public(current, redirected_headers)
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader('Location')
                    if not location:
                        raise FetchError(f'HTTP {response.status} without Location from {current}')
                    if redirect_count == MAX_REDIRECTS:
                        raise FetchError(f'more than {MAX_REDIRECTS} redirects from {url}')
                    current = urllib.parse.urljoin(current, location)
                    continue
                if response.status >= 400:
                    raise FetchError(f'HTTP {response.status} from {current}')
                ctype = response.getheader('Content-Type', '')
                raw = read_limited(response)
            finally:
                if response:
                    response.close()
                if conn:
                    conn.close()
            m = re.search(r'charset=([\w-]+)', ctype, re.I)
            enc = m.group(1) if m else 'utf-8'
            try:
                return raw.decode(enc, 'replace'), ctype
            except LookupError:
                return raw.decode('utf-8', 'replace'), ctype
    raise FetchError(f'could not fetch {url}')


def meta_tag(doc, *names):
    """First matching <meta> content, property= or name=, either attribute order."""
    for n in names:
        for pat in (rf'<meta[^>]+(?:property|name)=["\']{re.escape(n)}["\'][^>]*?content=["\']([^"\']*)["\']',
                    rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*?(?:property|name)=["\']{re.escape(n)}["\']'):
            m = re.search(pat, doc, re.I | re.S)
            if m and m.group(1).strip():
                return htmllib.unescape(m.group(1)).strip()
    return None


def scrape(url):
    doc, ctype = get(url)
    if 'html' not in ctype.lower() and '<html' not in doc[:2000].lower():
        return None, None
    title = meta_tag(doc, 'og:title', 'twitter:title')
    if not title:
        m = re.search(r'<title[^>]*>(.*?)</title>', doc, re.I | re.S)
        if m: title = htmllib.unescape(re.sub(r'\s+', ' ', m.group(1))).strip()
    desc = meta_tag(doc, 'og:description', 'twitter:description', 'description')
    return title, desc


def from_github(repo):
    tok = os.environ.get('GITHUB_TOKEN', '').strip()
    doc, _ = get(f'https://api.github.com/repos/{repo}',
                 accept='application/vnd.github+json',
                 headers=({'Authorization': 'Bearer ' + tok} if tok else None))
    d = json.loads(doc)
    return d.get('full_name') or repo, d.get('description')


def from_youtube(url):
    doc, _ = get('https://www.youtube.com/oembed?format=json&url=' + urllib.parse.quote(url, ''),
                 accept='application/json')
    d = json.loads(doc)
    return d.get('title'), (f"Video by {d['author_name']}." if d.get('author_name') else None)


def from_hn(item_id):
    """HN 429s aggressive page fetches; its Firebase API is public and unlimited."""
    def item(i):
        doc, _ = get(f'https://hacker-news.firebaseio.com/v0/item/{i}.json',
                     accept='application/json')
        return json.loads(doc) or {}
    d = item(item_id)
    # a link to a comment is a link to the discussion it sits in: climb to the
    # story so the entry is titled with the thread, not "Item"
    hops = 0
    while not d.get('title') and d.get('parent') and hops < 8:
        d = item(d['parent']); hops += 1
    title = d.get('title')
    if d.get('url'):
        try:
            t2, d2 = scrape(d['url'])
            return title or t2, d2      # the thread's title, the article's blurb
        except Exception:
            pass
    return title, (htmllib.unescape(re.sub(r'<[^>]+>', ' ', d.get('text') or '')) or None)


def from_twitter(status_id):
    """The tweet itself, via fxtwitter's public JSON.

    x.com serves a fetcher nothing but a JS shell, so the old fallback titled
    every post "Post by @handle" - useless in a search index, and outright wrong
    on a modern /i/status/ url where `i` is a placeholder rather than a handle.
    """
    doc, _ = get(f'https://api.fxtwitter.com/status/{status_id}',
                 accept='application/json')
    d = json.loads(doc)
    t = d.get('tweet') or {}
    a = t.get('author') or {}
    who = a.get('name') or a.get('screen_name') or ''
    handle = a.get('screen_name')
    text = re.sub(r'\s+', ' ', (t.get('text') or '')).strip()
    byline = f"Post by {who} (@{handle})" if handle else 'Post on X'
    if not text:                      # media-only post: the byline is all there is
        return byline, None
    title = text[:110].rstrip()
    if len(text) > 110:
        title = title.rsplit(' ', 1)[0] + '…'
    return title, (text if len(text) > len(title) else byline)


def from_arxiv(arxiv_id):
    """arXiv's Atom API, rather than scraping the abstract page."""
    doc, _ = get(f'https://export.arxiv.org/api/query?id_list={arxiv_id}',
                 accept='application/atom+xml')
    def tag(name):
        m = re.search(rf'<{name}>(.*?)</{name}>', doc, re.S)
        return htmllib.unescape(re.sub(r'\s+', ' ', m.group(1))).strip() if m else None
    # the feed repeats <title> for the query itself; the entry's is the second
    titles = re.findall(r'<title>(.*?)</title>', doc, re.S)
    title = htmllib.unescape(re.sub(r'\s+', ' ', titles[-1])).strip() if titles else None
    return title, tag('summary')


def from_wikipedia(host, path):
    slug = path.rstrip('/').split('/')[-1]
    doc, _ = get(f'https://{host}/api/rest_v1/page/summary/{slug}', accept='application/json')
    d = json.loads(doc)
    return d.get('title'), d.get('extract')


def fetch_meta(url):
    """(title, description). Raises on network failure so the caller can cache it."""
    host, repo = entity(url)
    path = re.sub(r'^https?://[^/]+', '', url).split('?')[0].split('#')[0]
    if repo:
        return from_github(repo)
    if host and re.search(r'(^|\.)(youtube\.com|youtu\.be)$', host):
        return from_youtube(url)
    if host and host.endswith('wikipedia.org') and '/wiki/' in path:
        return from_wikipedia(host, path)
    if host == 'news.ycombinator.com':
        m = re.search(r'[?&]id=(\d+)', url)
        if m: return from_hn(m.group(1))
    if host and re.search(r'(^|\.)arxiv\.org$', host):
        m = re.search(r'/(?:abs|pdf)/([\w.\-/]+?)(?:v\d+)?(?:\.pdf)?$', path)
        if m: return from_arxiv(m.group(1))
    # x/twitter and the vx/fx mirrors people paste are all the same post; the
    # status id is the only part of the path that reliably identifies it
    if host and re.search(r'(^|\.)((vx|fx)?twitter\.com|x\.com|fixupx\.com)$', host):
        u = re.search(r'/status(?:es)?/(\d+)', path)
        if u:
            try:
                return from_twitter(u.group(1))
            except Exception:
                pass                  # deleted, protected, or the mirror is down
        u = re.search(r'/([^/]+)/status', path)
        if u and u.group(1) not in ('i', 'web'):
            return f'Post by @{u.group(1)}', None
        return None, None
    return scrape(url)


def gaps(cache, path):
    """Links a fetch could not name. An agent that can read the page fills these."""
    out = {k: {'url': c.get('url'), 'error': c.get('error')}
           for k, c in cache.items() if not c.get('title')}
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f"{len(out)} links still unnamed -> {path}", file=sys.stderr)


def merge(cache, path):
    """Fold in {canonical: {title, description}} researched elsewhere."""
    with open(path, encoding='utf-8') as fh:
        got = json.load(fh)
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    n = 0
    for key, rec in got.items():
        title = (rec.get('title') or '').strip()
        if key not in cache or not title:
            continue
        c = cache[key]
        if c.get('title'):
            continue                      # never overwrite the page's own metadata
        desc = re.sub(r'\s+', ' ', rec.get('description') or '').strip()
        if BOILERPLATE.search(desc[:200]):
            desc = ''
        c.update({'ok': True, 'title': title[:160],
                  'description': tidy_desc(desc[:400]) or None,
                  'via': 'agent', 'fetchedAt': now})
        n += 1
    print(f"merged {n} researched links", file=sys.stderr)
    return n


def save_cache(cache):
    """Checkpoint atomically so a timeout never discards completed fetches."""
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, CACHE)


def main():
    args = sys.argv[1:]
    retry = '--retry' in args
    limit = int(args[args.index('--limit') + 1]) if '--limit' in args else None
    # a handler that got better needs its old answers thrown away, even the ones
    # that "worked" - matched against the stored url
    refetch = re.compile(args[args.index('--refetch') + 1], re.I) if '--refetch' in args else None

    if os.path.exists(CACHE):
        with open(CACHE, encoding='utf-8') as fh:
            cache = json.load(fh)
    else:
        cache = {}
    if '--gaps' in args:
        return gaps(cache, args[args.index('--gaps') + 1])
    if '--merge' in args:
        merge(cache, args[args.index('--merge') + 1])
        save_cache(cache)
        return print(f"wrote {CACHE}", file=sys.stderr)
    if not os.path.exists(HARVEST):
        raise SystemExit(f'no {HARVEST} - run fetch-discord.py first')
    with open(HARVEST, encoding='utf-8') as fh:
        harvest = json.load(fh)

    wanted = {}                     # canonical -> a real url to fetch
    for m in harvest['messages']:
        for u in m['urls']:
            if is_furniture(u['url']):
                continue
            k = canonical(u['url'])
            if k: wanted.setdefault(k, u['url'])

    now = datetime.now(timezone.utc)
    todo = []
    for key, url in wanted.items():
        c = cache.get(key)
        if not c:
            todo.append((key, url)); continue
        if refetch and refetch.search(c.get('url') or url):
            todo.append((key, url)); continue
        if c.get('ok') and (c.get('title') or not retry):
            continue
        if c.get('via') == 'agent' and c.get('title'):
            continue
        stale = now - datetime.fromisoformat(c['fetchedAt']) > timedelta(days=RETRY_AFTER_DAYS)
        if retry or stale:
            todo.append((key, url))
    if limit:
        todo = todo[:limit]

    ok = fail = processed = 0
    started = time.monotonic()
    for i, (key, url) in enumerate(todo, 1):
        if time.monotonic() - started >= TOTAL_BUDGET_SECONDS:
            print(f'\nstopped cleanly after {TOTAL_BUDGET_SECONDS}s; '
                  f'{len(todo) - processed} links remain for the next run', file=sys.stderr)
            break
        print(f"\r  {i}/{len(todo)} {url[:70]:<70}", end='', file=sys.stderr)
        rec = {'url': url, 'fetchedAt': now.isoformat(timespec='seconds')}
        try:
            title, desc = fetch_meta(url)
            rec['ok'] = True
            rec['title'] = re.sub(r'\s+', ' ', title).strip()[:160] if title else None
            desc = re.sub(r'\s+', ' ', desc).strip() if desc else ''
            if BOILERPLATE.search(desc[:200]):
                desc = ''
            rec['description'] = tidy_desc(desc[:400]) or None
            ok += 1
        except Exception as e:
            rec['ok'] = False
            rec['error'] = f'{type(e).__name__}: {e}'[:200]
            fail += 1
        cache[key] = rec
        save_cache(cache)
        processed += 1
        time.sleep(0.2)             # be a good citizen; this is not a crawler
    print(file=sys.stderr)

    # a canonicalisation change orphans the keys it used to produce; the harvest
    # only ever grows, so anything not wanted by it now is dead weight
    if '--prune' in args:
        dead = [k for k in cache if k not in wanted]
        for k in dead:
            del cache[k]
        print(f"pruned {len(dead)} orphaned cache keys", file=sys.stderr)

    save_cache(cache)

    titled = sum(1 for c in cache.values() if c.get('title'))
    blurbed = sum(1 for c in cache.values() if c.get('description'))
    print(f"\n=== LINK ENRICHMENT ===", file=sys.stderr)
    print(f"distinct links in harvest: {len(wanted)}   fetched now: {processed} "
          f"(ok {ok}, failed {fail})", file=sys.stderr)
    print(f"cache: {len(cache)} links, {titled} with a title, {blurbed} with a blurb",
          file=sys.stderr)
    print(f"wrote {CACHE} ({os.path.getsize(CACHE)/1024:.1f} KB)", file=sys.stderr)
    print(f"\nnext: python3 build-page.py", file=sys.stderr)


if __name__ == '__main__':
    main()
