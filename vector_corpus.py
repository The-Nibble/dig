"""Shared definition and identity of the corpus embedded for smart search."""
import hashlib
import json


def texts_for(entries):
    """Return the exact ordered strings embedded by Python and the web worker."""
    return [(entry['title'] + '. ' + (entry.get('description') or ''))[:400]
            for entry in entries]


def fingerprint(entries):
    """Identify both corpus content and order without ambiguous delimiters."""
    digest = hashlib.sha256()
    for text in texts_for(entries):
        encoded = text.encode('utf-8')
        digest.update(len(encoded).to_bytes(8, 'big'))
        digest.update(encoded)
    return digest.hexdigest()


def read_bootstrap(page):
    """Read the single-line BOOTSTRAP object from an index page."""
    with open(page, encoding='utf-8') as fh:
        for line in fh:
            if line.startswith('const BOOTSTRAP ='):
                return json.loads(line[len('const BOOTSTRAP ='):].strip().rstrip(';'))
    raise ValueError(f"could not read BOOTSTRAP from {page}")
