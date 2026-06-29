"""
Find the MPS ceiling: sweep precision x batch size x sequence length on the SAME texts.
Answers "are we getting the best of the GPU, or can we do better?" with numbers.

    python 01_sweep.py
    N=4000 python 01_sweep.py
"""

import os
import time

N = int(os.environ.get("N", "2500"))
MODEL = os.environ.get("MODEL", "intfloat/e5-small-v2")

print(f"Loading {N} trec-covid texts (long abstracts, the hard case)...")
from datasets import load_dataset

ds = load_dataset("BeIR/trec-covid", "corpus", split="corpus", streaming=True)
texts = []
for x in ds:
    texts.append("passage: " + ((x.get("title") or "") + " " + (x.get("text") or "")).strip())
    if len(texts) >= N:
        break
print(f"  loaded {len(texts):,} (avg {sum(len(t) for t in texts)//len(texts)} chars)\n")

import torch
from sentence_transformers import SentenceTransformer

print(f"torch {torch.__version__} | MPS {torch.backends.mps.is_available()}\n")


def run(device, batch, half=False, max_seq=None):
    m = SentenceTransformer(MODEL, device=device)
    if max_seq:
        m.max_seq_length = max_seq
    if half and device == "mps":
        m.half()
    # warmup
    m.encode(texts[:batch], batch_size=batch, normalize_embeddings=True, show_progress_bar=False)
    t = time.time()
    m.encode(texts, batch_size=batch, normalize_embeddings=True, show_progress_bar=False)
    return len(texts) / (time.time() - t)


CONFIGS = [
    ("MPS fp32  batch=128  seq=full", dict(device="mps", batch=128)),
    ("MPS fp32  batch=256  seq=full", dict(device="mps", batch=256)),
    ("MPS fp16  batch=256  seq=full", dict(device="mps", batch=256, half=True)),
    ("MPS fp16  batch=384  seq=full", dict(device="mps", batch=384, half=True)),
    ("MPS fp16  batch=256  seq=256 ", dict(device="mps", batch=256, half=True, max_seq=256)),
    ("MPS fp16  batch=256  seq=128 ", dict(device="mps", batch=256, half=True, max_seq=128)),
    ("CPU fp32  batch=128  seq=full", dict(device="cpu", batch=128)),
]

print(f"{'config':<34}{'docs/sec':>10}")
print("-" * 44)
best = ("", 0.0)
for name, kw in CONFIGS:
    try:
        r = run(**kw)
        if r > best[1]:
            best = (name, r)
        print(f"{name:<34}{r:>10,.0f}")
    except Exception as e:  # noqa: BLE001
        print(f"{name:<34}{('ERR: ' + str(e)[:24]):>10}")
print("-" * 44)
print(f"BEST: {best[0].strip()}  ->  {best[1]:,.0f} docs/sec")
print(f"vs in-Vespa CPU ~104 docs/sec  ->  {best[1]/104:.1f}x")
print(f"\nNote: shorter seq trades a little recall for speed; full-length is the safe default.")
