"""Does vectors.f32 still match the index inlined in index.html?

Exists so the daily job can decide whether to spend three minutes and a 5 MB
commit on rebuilding embeddings, without first installing onnxruntime to find
out. Stdlib only, for that reason.

Exit 0 = stale, rebuild needed.  Exit 1 = already matches.
"""
import json, os, sys

from vector_corpus import fingerprint, read_bootstrap

HERE = os.path.dirname(os.path.abspath(__file__))
DIM_BYTES = 384 * 4

try:
    data = read_bootstrap(os.path.join(HERE, 'index.html'))
except (OSError, ValueError, json.JSONDecodeError) as error:
    print(error, file=sys.stderr)
    raise SystemExit(0)                     # cannot prove fresh: rebuild
entries = data['entries']
want_rows = len(entries)
want_hash = fingerprint(entries)
if data.get('vectorHash') != want_hash:
    print('index vectorHash does not match its embedding inputs', file=sys.stderr)
    raise SystemExit(0)

vec = os.path.join(HERE, 'vectors.f32')
meta_path = os.path.join(HERE, 'vectors.json')
try:
    meta = json.load(open(meta_path, encoding='utf-8'))
except (OSError, json.JSONDecodeError):
    meta = {}
size = os.path.getsize(vec) if os.path.exists(vec) else 0
fresh = (size == want_rows * DIM_BYTES
         and meta == {'hash': want_hash, 'rows': want_rows, 'dim': 384})
print(f'index has {want_rows} entries with vector hash {want_hash}', file=sys.stderr)
print(f'vectors.f32 has {size} bytes; metadata hash is {meta.get("hash")}', file=sys.stderr)
raise SystemExit(1 if fresh else 0)
