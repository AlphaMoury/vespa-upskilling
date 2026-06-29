# Vespa use-case demo — one engine, three indexes

An interactive demo that switches between **three Vespa indexes / use cases** and shows
**keyword vs. AI-hybrid** search side by side, with **typeahead** and (for catering) **filters**.

```
React (Vite, :5173) ──► FastAPI proxy (:8009) ──► Vespa (:8080), three schemas:
  index tabs              /api/typeahead            · dish     — EzCater catering (commerce)
  keyword | hybrid        /api/search               · covid    — trec-covid papers (medical)
  filters (catering)     (query-embed, dedupe,      · question — Quora questions (Q&A, volume)
  typeahead               per-index filters)
```

Three use cases, **one engine**: catering commerce search, medical research retrieval, and
high-volume Q&A — all with the same hybrid (BM25 ⊕ e5 vectors, fused with reciprocal rank fusion).

## Run it

Prereqs: Docker running, capstone venv (`../capstone/setup.sh`), Node, FastAPI in the venv
(`uv pip install --python ../capstone/.venv/bin/python fastapi "uvicorn[standard]"`).

```bash
python data/build_dataset.py                 # once: generate catering data
bash run.sh                                  # deploy+feed all 3 indexes, start API + UI
# open http://localhost:5173
```

Feed sizes are configurable: `COVID_N=50000 QUORA_N=150000 ...` (defaults). The full-scale
run earlier fed **522,931** Quora docs on a laptop — these are fast subsets for a live demo.

Manual pieces:
```bash
../capstone/.venv/bin/python deploy_and_feed.py                      # deploy + feed Vespa
cd server && ../../capstone/.venv/bin/python -m uvicorn main:app --port 8009   # API
cd web && npm install && npm run dev                                 # UI on :5173
```

## Demo script (the "different use cases" story)

Use the **tabs** to switch index. For each, type in the box (live typeahead) then run a query.

1. **❓ Quora questions** — the killer contrast. Search **"how do I become a better programmer"**:
   - *Keyword:* "How do I become a politician / data scientist / billionaire" (matches "how do I become a").
   - *Hybrid:* "How do I become a **great programmer**" — understands the meaning.
2. **🍽️ Catering** — type `chick`, `med`, `tac` (typeahead). Search **"gluten free options"**:
   keyword finds nothing; hybrid finds gluten-free dishes. Then use the **filters** (cuisine,
   vegan/gluten-free chips, price) to narrow live.
3. **🦠 COVID research** — search **"airborne transmission of respiratory viruses"**; hybrid
   pulls the on-topic papers. Note the index size (50,000 papers).

The point: **one Vespa engine** does typeahead + keyword + semantic + ranking + filtering across
three very different datasets — the consolidation play, and it scaled to 522k docs on a laptop.

## Files
- `data/build_dataset.py` — generates catering `dishes.jsonl` (real dish names).
- `app_package.py` — the three Vespa schemas (dish / covid / question) + rank profiles + gram typeahead.
- `deploy_and_feed.py` — deploy + feed all three (catering jsonl, trec-covid, quora).
- `server/main.py` — FastAPI proxy (per-index typeahead + keyword/semantic/hybrid + filters).
- `web/` — the React (Vite) app (index tabs, filters, split-view).

> Local-only (the UI needs the FastAPI + Vespa backend) — present it from your machine.
