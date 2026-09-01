#!/usr/bin/env python3
"""Precompute corpus embeddings so the browser skips the first-load embed.

Uses the SAME quantized ONNX model the page loads at runtime, so the shipped doc
vectors live in the same space as the query vectors transformers.js produces.
Reads the entries inlined in index.html, writes vectors.f32 (raw little-endian
float32, N * 384, in entry order) next to it.

Deps (not stdlib): onnxruntime, tokenizers, numpy. Run after build-index.py:
    python3 build-vectors.py
"""
import json, os, sys, random
import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

from vector_corpus import fingerprint, read_bootstrap, texts_for

HERE = os.path.dirname(os.path.abspath(__file__))
M = os.path.join(HERE, 'models', 'Xenova', 'all-MiniLM-L6-v2')
DIM = 384

# Entries are embedded in the exact order inlined in index.html. The shared
# fingerprint prevents a same-sized but reordered or edited corpus from being
# paired with stale vectors.
data = read_bootstrap(os.path.join(HERE, 'index.html'))
entries = data['entries']
texts = texts_for(entries)
corpus_hash = fingerprint(entries)
if data.get('vectorHash') != corpus_hash:
    raise SystemExit('index.html vectorHash does not match its embedding inputs; '
                     'run build-page.py first')

tok = Tokenizer.from_file(os.path.join(M, 'tokenizer.json'))
tok.enable_truncation(max_length=256)
tok.enable_padding()
sess = ort.InferenceSession(os.path.join(M, 'onnx', 'model_quantized.onnx'),
                            providers=['CPUExecutionProvider'])

def embed(batch):
    encs = tok.encode_batch(batch)
    ids = np.array([e.ids for e in encs], dtype=np.int64)
    mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
    types = np.zeros_like(ids)
    out = sess.run(['last_hidden_state'],
                   {'input_ids': ids, 'attention_mask': mask, 'token_type_ids': types})[0]
    m = mask[:, :, None].astype(np.float32)          # mean-pool over real tokens
    pooled = (out * m).sum(1) / np.clip(m.sum(1), 1e-9, None)
    pooled /= np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9, None)  # L2 norm
    return pooled.astype(np.float32)

if '--probe' in sys.argv:
    # Calibration for the ABS_FLOOR constant in index.html: embed real queries and
    # deliberate gibberish against the shipped vectors, and read off the gap. The
    # floor has to sit above what nonsense scores, or the empty state never fires.
    vecs = np.fromfile(os.path.join(HERE, 'vectors.f32'), dtype=np.float32).reshape(-1, DIM)
    assert len(vecs) == len(entries), f'vectors.f32 has {len(vecs)} rows, index has {len(entries)}'
    # Deliberately generic: queries picked by looking at this corpus would make the
    # calibration circular. What actually sets the bar is the gibberish ceiling,
    # which is a property of the tokenizer and model rather than of the archive -
    # WordPiece shreds a keyboard mash into subwords that still land somewhere.
    real = ['how do i make a website faster', 'lightweight database',
            'command line productivity', 'writing documentation',
            'colour and contrast', 'keeping notes in plain text',
            'debugging memory leaks', 'learning a new language',
            'drawing diagrams', 'reading source code', 'spaced repetition',
            'why is my build slow', 'typed configuration files',
            'small self hosted services', 'privacy preserving analytics',
            'generative models for images', 'streaming data pipelines',
            'accessible forms', 'time zones are hard', 'compiler optimisations']
    rng = random.Random(7)
    cons, vows = 'bcdfghjklmnpqrstvwxz', 'aeiou'
    junk = ['asdfkjh', 'qwertyuiop', 'zzzzzz', 'xkcdvbnm', 'plfjqwoeiru',
            'hjklasdf', 'mnbvcxz', 'wertyuio', 'qazwsxedc', 'lkjhgfdsa']
    # random mashes as well, so the ceiling is not an artefact of five hand-typed strings
    junk += [''.join(rng.choice(cons if i % 3 else vows) for i in range(rng.randint(5, 11)))
             for _ in range(20)]

    print(f'{"query":34} {"top1":>6} {"top5":>6} {"top20":>6} {"top50":>6}')
    def row(label, q):
        sims = np.sort(embed([q])[0] @ vecs.T)[::-1]
        print(f'{label:34} {sims[0]:6.3f} {sims[4]:6.3f} {sims[19]:6.3f} {sims[49]:6.3f}')
        return sims
    rs = [row(q, q) for q in real]
    print()
    js = [row('JUNK ' + q, q) for q in junk]
    print()
    tops = sorted((s[0] for s in js), reverse=True)
    print(f'\ngibberish top-1: max {tops[0]:.3f}  p90 {np.percentile(tops, 90):.3f}  '
          f'median {np.median(tops):.3f}   (n={len(tops)})')
    print(f'lowest top-1 across real queries          : {min(s[0] for s in rs):.3f}')
    print(f'median real top-20                        : {np.median([s[19] for s in rs]):.3f}')
    for f in (0.25, 0.30, 0.35, 0.40, 0.45):
        kept = [int((s >= f).sum()) for s in rs]
        jk = [int((s >= f).sum()) for s in js]
        print(f'  floor {f:.2f}: real queries keep median {int(np.median(kept)):4d} '
              f'(min {min(kept):3d}) | gibberish keeps max {max(jk):3d}')
    sys.exit(0)

vecs = np.zeros((len(texts), DIM), dtype=np.float32)
B = 64
for i in range(0, len(texts), B):
    vecs[i:i+B] = embed(texts[i:i+B])
    print(f"\rembedded {min(i+B, len(texts))}/{len(texts)}", end='', file=sys.stderr)
print(file=sys.stderr)

out_path = os.path.join(HERE, 'vectors.f32')
vecs.tofile(out_path)
print(f"wrote {out_path}: {vecs.shape} -> {os.path.getsize(out_path)/1024/1024:.2f} MB", file=sys.stderr)

meta_path = os.path.join(HERE, 'vectors.json')
tmp_path = meta_path + '.tmp'
with open(tmp_path, 'w', encoding='utf-8') as fh:
    json.dump({'hash': corpus_hash, 'rows': len(entries), 'dim': DIM}, fh,
              separators=(',', ':'))
    fh.write('\n')
os.replace(tmp_path, meta_path)
print(f"wrote {meta_path}: {corpus_hash}", file=sys.stderr)

# sanity: norms ~1, and a couple of nearest neighbours make sense
norms = np.linalg.norm(vecs, axis=1)
print(f"norm min/max: {norms.min():.3f}/{norms.max():.3f}", file=sys.stderr)
