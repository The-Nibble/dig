"""Does vectors.f32 still match the index inlined in index.html?

Exists so the daily job can decide whether to spend three minutes and a 5 MB
commit on rebuilding embeddings, without first installing onnxruntime to find
out. Stdlib only, for that reason.

Exit 0 = stale, rebuild needed.  Exit 1 = already matches.
"""
import json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
DIM_BYTES = 384 * 4

html = open(os.path.join(HERE, 'index.html'), encoding='utf-8').read()
m = re.search(r'const BOOTSTRAP = (\{.*?\});\n', html)
if not m:
    print('could not read BOOTSTRAP from index.html', file=sys.stderr)
    raise SystemExit(0)                     # cannot prove fresh: rebuild
want = len(json.loads(m.group(1))['entries'])

vec = os.path.join(HERE, 'vectors.f32')
rows = os.path.getsize(vec) // DIM_BYTES if os.path.exists(vec) else 0
print(f'index has {want} entries, vectors.f32 holds {rows} rows', file=sys.stderr)
raise SystemExit(1 if rows == want else 0)
