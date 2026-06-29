# EzCater × Vespa — catering search demo

An EzCater-flavored demo: **live typeahead** + **keyword vs. AI-hybrid** search, side by side,
on a catering dataset. Built to show the business value of Vespa for catering discovery.

```
React (Vite, :5173)  ──►  FastAPI proxy (:8009)  ──►  Vespa (:8080)
  typeahead bar             /api/typeahead             two indexes:
  keyword | hybrid          /api/search                  · caterer (suppliers)
  split view               (query-embed + dedupe)        · dish    (menu items)
```

- **Two indexes** = EzCater's marketplace shape: caterers and the dishes they offer.
- **Typeahead** uses Vespa n-gram matching (`grams` field) — substring, mid-word, case-insensitive.
- **Hybrid** = BM25 ⊕ e5 vectors fused with reciprocal rank fusion (the `hybrid` rank profile).

## Run it

Prereqs: Docker running, the capstone venv built (`../capstone/setup.sh`), Node installed.

```bash
# 0) generate the dataset (once)
python data/build_dataset.py

# 1) everything (deploy+feed Vespa, start API, start UI):
bash run.sh
# then open http://localhost:5173
```

Or run the pieces manually:
```bash
../capstone/.venv/bin/python deploy_and_feed.py                       # deploy + feed Vespa
cd server && ../../capstone/.venv/bin/python -m uvicorn main:app --port 8009   # API
cd web && npm install && npm run dev                                  # UI on :5173
```

## Demo script (what to show)

1. **Typeahead** — type `chick`, `med`, `tac`, `bbq`, `mango` → instant dish suggestions.
2. **Natural-language queries** (click the example chips) — watch the two columns:
   - `gluten free options` → **keyword finds nothing**, hybrid finds gluten-free dishes.
   - `something spicy for the team` → keyword → bagels; hybrid → **Mapo Tofu**.
   - `healthy plant-based lunch for a client meeting` → hybrid surfaces vegan bowls/salads.
   - `light bites for a morning meeting` → hybrid → bagels, pastries, parfaits.
3. The point: **one Vespa engine** does typeahead + keyword + semantic + ranking over two indexes —
   the consolidation play, on EzCater's actual problem (catering discovery).

## Files
- `data/build_dataset.py` — generates `caterers.jsonl` + `dishes.jsonl` (real dish names, synthetic caterers).
- `app_package.py` — the two Vespa schemas (caterer + dish) + rank profiles + gram typeahead field.
- `deploy_and_feed.py` — deploy to Docker + feed both indexes.
- `server/main.py` — FastAPI proxy (typeahead, keyword/semantic/hybrid search).
- `web/` — the React (Vite) app.

> This demo runs locally (the UI needs the FastAPI + Vespa backend). It is not a static site.
