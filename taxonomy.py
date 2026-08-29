"""Shared vocabulary for every source that feeds the index.

The Nibble parser and the Discord fetcher have to agree on what a tag means and
on when two links are the same link, or the merged index tags the same tool two
different ways and shows it twice. Both import from here.
"""
import re
from urllib.parse import urlparse, urlsplit, urlunsplit, parse_qsl, urlencode

# kind slug -> (display label, is it timeless?)
KIND_META = {'news': ('News', False), 'tool': ('Tool', True), 'read': ('Read', True),
             'quote': ('Quote', True), 'til': ('TIL', True), 'awe': ('Curiosity', True)}

TAG_RULES = [  # (regex over haystack, slug, label, facet)
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
for _, _s, _l, _f in TAG_RULES: TAG_META[_s] = (_l, _f)
for _k, (_l, _t) in KIND_META.items(): TAG_META[_k] = (_l, 'kind')
TAG_META['wikipedia'] = ('Wikipedia', 'source')
TAG_META['video'] = ('Video', 'source')
TAG_META['discord'] = ('Discord', 'source')

GH_RESERVED = {'apps', 'sponsors', 'marketplace', 'settings', 'about', 'features', 'topics',
               'collections', 'notifications', 'orgs', 'users', 'login', 'join', 'pricing',
               'site', 'security', 'explore', 'trending', 'new', 'organizations', 'dashboard',
               'stars', 'issues', 'pulls', 'codespaces', 'readme', 'search'}


def entity(url):
    """(host, owner/repo) - repo only when the url is a GitHub project root."""
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


# Chat furniture: uploads, gifs and links back into Discord itself. Applied at
# harvest time AND at build time, so widening this list re-cleans an existing
# harvest instead of needing the whole channel re-fetched.
SKIP_HOST = re.compile(
    r'^(cdn\.discordapp\.com|media\.discordapp\.net|discord\.(com|gg)|discordapp\.com'
    r'|tenor\.com|media\.tenor\.com|giphy\.com|media\d*\.giphy\.com'
    r'|images-ext-\d+\.discordapp\.net)$', re.I)


def is_furniture(url):
    host, _ = entity(url)
    return bool(host and SKIP_HOST.match(host))


# Job postings are temporal in exactly the way News is, and News is deliberately
# left out of this archive: a posting 404s within months, and "when did we first
# talk about X" is not a question anyone asks of a vacancy. Applicant-tracking
# hosts are unambiguous; the path rule is anchored at the start so an article at
# businessinsider.in/tech/careers/... is not mistaken for a vacancy.
ATS_HOST = re.compile(
    r'(^|\.)(boards\.greenhouse\.io|greenhouse\.io|jobs\.lever\.co|lever\.co|jobs\.ashbyhq\.com'
    r'|ashbyhq\.com|apply\.workable\.com|workable\.com|smartrecruiters\.com|recruitee\.com'
    r'|breezy\.hr|[\w-]+\.myworkdayjobs\.com|wellfound\.com|angel\.co|[\w-]+\.hire\.trakstar\.com'
    r'|rubyonremote\.com|aijobs\.app|web3\.career|ats\.rippling\.com|jobs\.apple\.com'
    r'|flipkartcareers\.com|naukri\.com|hirist\.com|instahyre\.com|cutshort\.io)$', re.I)
_JOB_PATH = re.compile(r'^/(careers?|jobs)(/|$)', re.I)


def is_job(url):
    host, _ = entity(url)
    if host and ATS_HOST.search(host):
        return True
    try:
        return bool(_JOB_PATH.match(urlparse(url).path or ''))
    except Exception:
        return False


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


# ---- canonical url --------------------------------------------------------
# The same tool arrives spelled a dozen ways: with utm tags off a newsletter,
# as a youtu.be short link, with or without www or a trailing slash. Dedupe is
# only as good as this function, so it is deliberately conservative - it strips
# things that provably never change what you land on, and leaves the rest.

# tracking params, not addressing params: dropping these cannot change the page
_JUNK_PARAMS = re.compile(
    r'^(utm_[a-z_]+|ref|ref_src|ref_url|referrer|source|src|fbclid|gclid|mc_cid|mc_eid'
    r'|igshid|si|feature|spm|_hsenc|_hsmi|hss_channel|at_medium|at_campaign|sk|share)$', re.I)

# Non-junk query params are KEPT. On plenty of hosts the query string is the
# whole address - the video on youtube.com/watch, the app on play.google.com,
# the thread on news.ycombinator.com/item - and dropping it merges unrelated
# entries into one. A false split leaves a visible duplicate; a false merge
# silently deletes an entry. Splitting is the safer failure.


def canonical(url):
    """A dedupe key, or None when the url addresses nothing groupable.

    Same key == same destination. Not a fetchable url. Returning None for
    host-less urls matters: bare '#footnote-1' anchors would otherwise all
    share the empty key and collapse into a single entry.
    """
    if not url: return None
    try:
        p = urlsplit(url.strip())
    except Exception:
        return None
    host = (p.netloc or '').lower().split('@')[-1].split(':')[0]
    if not host: return None
    if host.startswith('www.'): host = host[4:]
    if host.startswith('m.'): host = host[2:]
    path = p.path or '/'

    # youtu.be/ID and youtube.com/watch?v=ID are the same video
    if host == 'youtu.be':
        vid = path.strip('/').split('/')[0]
        if vid: return f'youtube.com/watch?v={vid}'
    if host in ('youtube.com', 'music.youtube.com') and path.rstrip('/') == '/watch':
        vid = dict(parse_qsl(p.query)).get('v')
        if vid: return f'youtube.com/watch?v={vid}'

    # one tweet, however it was spelled. x.com and twitter.com serve the same
    # post, the vx/fx mirrors exist only to render it, and the handle in the
    # path is not even required to be correct - the status id is the identity.
    if re.match(r'^((vx|fx)?twitter\.com|x\.com|fixupx\.com)$', host):
        m = re.search(r'/status(?:es)?/(\d+)', path)
        if m:
            return f'x.com/status/{m.group(1)}'

    # a GitHub project is one entity however deep the link goes
    if host == 'github.com':
        parts = [x for x in path.split('/') if x]
        if len(parts) >= 2 and parts[0].lower() not in GH_RESERVED:
            return f"github.com/{parts[0].lower()}/{re.sub(r'.git$', '', parts[1]).lower()}"

    keep = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
            if not _JUNK_PARAMS.match(k)]
    query = urlencode(sorted(keep))

    path = re.sub(r'/index\.(html?|php)$', '/', path)
    path = path.rstrip('/') or '/'
    # case-sensitivity is real on some paths, but cross-source dupes are far
    # more common than two urls differing only by case
    return urlunsplit(('', host, path.lower(), query.lower(), '')).lstrip('/')


# ---- description tidying --------------------------------------------------
_LEAD = re.compile(r'^[\s.,;:!?)\]…–—-]+')
_CONN = re.compile(r'^(and|but|so|also|plus|yet)\b[\s,]*', re.I)


def tidy_desc(d):
    """Strip stray lead-in punctuation/conjunctions, capitalise, end with a stop."""
    if not d: return ''
    d = _CONN.sub('', _LEAD.sub('', d)).strip()
    if d:
        d = d[0].upper() + d[1:]
        if d[-1] not in '.!?)”"':
            d += '.'
    return d
