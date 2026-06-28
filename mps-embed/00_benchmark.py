"""
MPS embedding benchmark — can the Apple GPU embed faster than Vespa's in-container CPU?

Context: feeding trec-covid through Vespa-in-Docker, where Vespa embeds each doc on ingest,
we measured ~104 docs/sec (CPU-bound). Here we embed the SAME model (e5-small-v2) on the
macOS HOST with PyTorch — on the Apple GPU (device="mps") vs the host CPU — to see if moving
embedding off Vespa and onto the GPU is worth it.

    python 00_benchmark.py
    N=6000 BATCH=256 python 00_benchmark.py        # bigger sample / batch
    MODEL=intfloat/e5-base-v2 python 00_benchmark.py

Output: docs/sec on MPS and CPU, the speedup vs the in-Vespa baseline, and projected
embed time for 171k and 524k docs.
"""

import os
import time

N = int(os.environ.get("N", "3000"))
BATCH = int(os.environ.get("BATCH", "128"))
MODEL = os.environ.get("MODEL", "intfloat/e5-small-v2")
VESPA_CPU_BASELINE = 104  # docs/sec measured feeding trec-covid through Vespa-in-Docker

print(f"Loading {N} texts from trec-covid (uses the HF cache you already downloaded)...")
from datasets import load_dataset

ds = load_dataset("BeIR/trec-covid", "corpus", split="corpus", streaming=True)
texts = []
for x in ds:
    t = ((x.get("title") or "") + " " + (x.get("text") or "")).strip()
    texts.append("passage: " + t)  # e5 is trained with a "passage:" prefix for documents
    if len(texts) >= N:
        break
avg = sum(len(t) for t in texts) // max(len(texts), 1)
print(f"  loaded {len(texts):,} texts (avg {avg} chars)\n")

import torch
from sentence_transformers import SentenceTransformer

print(f"torch {torch.__version__}  |  MPS available: {torch.backends.mps.is_available()}\n")


HALF = os.environ.get("HALF", "0") == "1"  # try fp16 on MPS (HALF=1)


def bench(device):
    model = SentenceTransformer(MODEL, device=device)
    if HALF and device == "mps":
        model.half()  # fp16 — can speed up MPS a lot for transformer inference
    # warmup: first call compiles Metal kernels / pages weights — don't time it
    model.encode(texts[:BATCH], batch_size=BATCH, normalize_embeddings=True, show_progress_bar=False)

    # encode in chunks so we can print LIVE docs/sec while it runs
    CHUNK = max(BATCH, 500)
    done, t0, t_last, n_last, dim = 0, time.time(), time.time(), 0, None
    for i in range(0, len(texts), CHUNK):
        chunk = texts[i:i + CHUNK]
        emb = model.encode(chunk, batch_size=BATCH, normalize_embeddings=True, show_progress_bar=False)
        dim = emb.shape[1]
        done += len(chunk)
        now = time.time()
        inst = (done - n_last) / max(now - t_last, 1e-6)
        print(f"    [{device.upper()}{' fp16' if (HALF and device=='mps') else ''}] {done:>6,}/{len(texts):,}   {inst:7,.0f} docs/sec (live)")
        t_last, n_last = now, done

    dt = time.time() - t0
    rate = len(texts) / dt
    print(f"  {device.upper():>4} TOTAL:  {len(texts):,} docs in {dt:6.1f}s   ->   {rate:8,.0f} docs/sec   (dim {dim})")
    return rate


rates = {}
if torch.backends.mps.is_available():
    rates["mps"] = bench("mps")
else:
    print("  (MPS not available — running CPU only)")
rates["cpu"] = bench("cpu")

best_dev = max(rates, key=rates.get)
best = rates[best_dev]
print("\n==================== verdict ====================")
print(f"  Fastest host backend : {best_dev.upper()} @ {best:,.0f} docs/sec")
print(f"  Vespa in-Docker (CPU): ~{VESPA_CPU_BASELINE} docs/sec  (embeds on ingest)")
print(f"  Host {best_dev.upper()} speedup    : {best / VESPA_CPU_BASELINE:.1f}x faster than in-Vespa embedding")
if "mps" in rates and "cpu" in rates:
    print(f"  MPS vs host CPU      : {rates['mps'] / rates['cpu']:.1f}x")
print()
for n in (171_332, 524_000):
    print(f"  Embed {n:>7,} docs on {best_dev.upper()}: ~{n / best / 60:4.1f} min   (vs ~{n / VESPA_CPU_BASELINE / 60:3.0f} min in-Vespa)")
print("=================================================")
print("\nIf MPS wins big: precompute vectors here, feed them to Vespa as a normal field")
print("(no in-engine embed step) -> ingest is then bound by I/O + HNSW build, not the CPU embedder.")
