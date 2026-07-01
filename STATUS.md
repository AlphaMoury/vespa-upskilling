# Project status & recap

**What this is:** a Vespa upskilling repo that grew into **interview prep for the ezCater
"ML/LLM Search Specialist" role** (their "Nova" replatform onto **Vespa**). It contains a
learning pack, a scaled Vespa capstone, and a working **ezCater-style search demo**
(hybrid search + typeahead + food-ontology + LLM query understanding).

Repo: https://github.com/AlphaMoury/vespa-upskilling · Pages: https://alphamoury.github.io/vespa-upskilling/

---

## The stack (technologies)

| Layer | Tech |
|---|---|
| Search engine | **Vespa** (in Docker, `vespaengine/vespa`) — BM25 + **HNSW** ANN + **hybrid** (reciprocal rank fusion), gram **typeahead**, phased ranking |
| Embeddings | **e5-small-v2** (int8 ONNX) running **inside Vespa** (hugging-face-embedder), 384-dim |
| Vespa client | **pyvespa** (Python) — schema, deploy, feed, query; also the `vespa` CLI (`vespacli`) |
| API / proxy | **FastAPI + uvicorn** (Python) — typeahead, keyword/semantic/hybrid, **query understanding**, filters, CORS |
| Frontend | **React + Vite** (Node) — index switcher, keyword-vs-hybrid split view, filters |
| Datasets | **BeIR/trec-covid** (171k), **BeIR/quora** (523k), synthetic catering (600 dishes / 100 caterers) |
| Env / infra | **uv** (Python 3.13 venvs), **Docker** (bumped to ~14 GB), macOS |
| Extras | **torch + sentence-transformers** (MPS embedding benchmark); optional **Anthropic LLM** for query understanding / ontology (heuristic fallback if no key) |

---

## What's in the repo

- **`docs/`** — learning pack: `01-deep-dive`, `02-study-plan-48h`, `03-cheatsheet`,
  `04-pro-deep-dive`, `05-advanced-labs`.
- **`capstone/`** — pyvespa hybrid search on BeIR/NFCorpus: deploy → feed → compare
  keyword/semantic/hybrid → **nDCG@10** (0.74 vs 0.59 vs 0.51 on trec-covid). Scale-tested to **522,931 docs** locally.
- **`native-app/`** — the same app as raw `services.xml` + `.sd` (vespa CLI). **`pro-java-searcher/`** — a Java Searcher. **`mps-embed/`** — host-GPU vs in-Vespa embedding benchmark (finding: ties ~100 docs/sec).
- **`slides/vespa-tto.html`** — 2-slide deck (reframed: "ezCater already runs Vespa").
- **`ezcater-demo/`** — ⭐ the main demo (see below).
- **`ezcater-demo/POSITIONING.md`** — verified intel: ezCater runs Vespa (Go/Temporal/Kafka), their stack, discovery gaps, business value.

## The ezCater demo (`ezcater-demo/`)

- **One Vespa app, three indexes** (three use cases): `dish` (catering), `covid` (research), `question` (Quora).
- **FastAPI proxy** (`server/main.py`): `/api/typeahead`, `/api/search` (keyword|semantic|hybrid|**understood**), `/api/understand` (NL → structured concepts), per-index filters.
- **React UI** (`web/`): tab switcher, keyword-vs-hybrid split, filters.
- **Food ontology** (`data/build_dataset.py` `enrich()`): dishes enriched with spice_level, flavor, occasion, ingredients, allergens, price/head — the "backend data enrichment" use case.
- **Query understanding** (`server/main.py`): NL query → `{dietary, exclude_allergens, spice_min, cuisine, max_price_pp, headcount, ...}` → precise Vespa query — the "frontend query understanding" use case. LLM (Anthropic) if `ANTHROPIC_API_KEY`, else a heuristic parser.

---

## How to start it

```bash
# 1) Docker must be running. Vespa container "ezcater" holds the data.
docker start ezcater           # if it's stopped; skip if already up

# 2) start the demo (fast — skips re-feeding if data is already loaded):
cd ezcater-demo
bash run.sh
#   -> API on http://localhost:8009,  UI on http://localhost:5173  (open the UI)

# Rebuild everything from scratch (deploy + re-feed all indexes, ~40 min):
FRESH=1 bash run.sh
```

Stop: Ctrl-C the `run.sh` (stops API + UI). `docker stop ezcater` pauses Vespa (data persists);
`docker start ezcater` resumes (~20 s). Only a full `docker rm` deletes the indexed data.

> Note: plain `bash run.sh` used to re-deploy + re-embed every time — that's fixed; it now
> skips straight to the API+UI when the Vespa container already has data.

---

## Current state (2026-07-01)

- Vespa container **up**, full data: dish **600** · covid **171,332** · quora **522,931**.
- **Backend** (FastAPI) has the food-ontology + query-understanding wired and tested.
- **React UI** is still the 3-index keyword-vs-hybrid version — the **query-understanding UI is not wired yet** (next step).

## Roadmap / open decisions

1. **Data realism** — mirror ezCater's *catering* catalog (platters/trays/serves-N/per-head price), grounded in real ingredients (Option A: reframe real recipes; Option B: LLM-generate on-brand catalog). *Decision pending.*
2. **Real LLM ontology** (`build_ontology.py`) — LLM extracts ingredients + **infers** dietary/allergens (the tag-accuracy fix), optionally cross-referenced with Open Food Facts.
3. **React query-understanding UI** — show extracted concepts + "filters you'd set" vs "what we understood."
4. **Interview-prep doc** — Vespa pros/cons, two-tower/embeddings, LightGBM ranking, KG/ontology tech (Neo4j/RDF/GraphRAG), caching/latency, A/B + position bias.
5. Optional v2: geo delivery-radius, ranking contexts, real-time availability.
