#!/usr/bin/env python3
"""Precompute corpus embeddings so the browser skips the first-load embed.

Uses the SAME quantized ONNX model the page loads at runtime, so the shipped doc
vectors live in the same space as the query vectors transformers.js produces.
Reads the entries inlined in index.html, writes vectors.f32 (raw little-endian
float32, N * 384, in entry order) next to it.

Deps (not stdlib): onnxruntime, tokenizers, numpy. Run after build-index.py:
    python3 build-vectors.py
"""
import re, json, os, sys
import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

HERE = os.path.dirname(os.path.abspath(__file__))
M = os.path.join(HERE, 'models', 'Xenova', 'all-MiniLM-L6-v2')
DIM = 384

# entries, in the exact order they are inlined in index.html
html = open(os.path.join(HERE, 'index.html'), encoding='utf-8').read()
data = json.loads(re.search(r'const BOOTSTRAP = (\{.*?\});\n', html).group(1))
entries = data['entries']
# must match the worker: (title + '. ' + (description||'')).slice(0, 400)
texts = [((e['title'] + '. ' + (e.get('description') or ''))[:400]) for e in entries]

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

vecs = np.zeros((len(texts), DIM), dtype=np.float32)
B = 64
for i in range(0, len(texts), B):
    vecs[i:i+B] = embed(texts[i:i+B])
    print(f"\rembedded {min(i+B, len(texts))}/{len(texts)}", end='', file=sys.stderr)
print(file=sys.stderr)

out_path = os.path.join(HERE, 'vectors.f32')
vecs.tofile(out_path)
print(f"wrote {out_path}: {vecs.shape} -> {os.path.getsize(out_path)/1024/1024:.2f} MB", file=sys.stderr)

# sanity: norms ~1, and a couple of nearest neighbours make sense
norms = np.linalg.norm(vecs, axis=1)
print(f"norm min/max: {norms.min():.3f}/{norms.max():.3f}", file=sys.stderr)
