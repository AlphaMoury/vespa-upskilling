# MPS embedding experiment (separate project)

**Question:** Vespa-in-Docker embeds on CPU at ~104 docs/sec (that's the ingest bottleneck).
Can we go faster by embedding on the **Apple GPU (Metal/MPS)** on the host instead?

**Why Vespa can't use MPS itself:** it runs in a Linux Docker container (no GPU passthrough on
macOS), and its GPU path is CUDA/NVIDIA, not Metal. So to use the Apple GPU we move embedding
*out* of Vespa: embed on the host with PyTorch, then feed **precomputed vectors** to Vespa as a
normal document field (no `embed` step in the schema).

```
mps-embed/
├── requirements.txt
├── 00_benchmark.py     ← the gate: MPS vs CPU docs/sec, vs the in-Vespa baseline
├── app_package.py      ← (added if benchmark wins) schema with embedding as a FED field
├── 01_embed_and_feed.py← (added) host-embed on MPS + feed vectors to Vespa
└── 02_search.py        ← (added) embed query on host, pass the vector
```

## Setup & run the benchmark

```bash
cd mps-embed
uv venv --python 3.12 .venv          # torch wheels are happiest on 3.12
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python 00_benchmark.py     # prints MPS vs CPU docs/sec + projections
```

## Trade-off (know this as a pro)
- **In-engine embed (the capstone):** simplest, portable, query with plain text via `embed()`.
  Embedding runs on Vespa's CPU/CUDA.
- **Host precompute (this project):** use any accelerator (MPS), much faster bulk ingest, and you
  can add the correct `query:`/`passage:` e5 prefixes — but you now own the model + batching, and
  prod must reproduce the exact embedding. Best when ingest throughput matters.

If the benchmark shows a big MPS win, the plan is: precompute + feed, then push to **524k docs (quora)**.
