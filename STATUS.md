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
| Datasets | **BeIR/trec-covid** (171k), **BeIR/quora** (523k), **Food.com recipes** (real, HF), synthetic catering, menu **PDFs** |
| Ingestion | pluggable **`SourceAdapter`s** → one `MenuItem` schema → shared **food-ontology enrichment** → Vespa (see `ingest/`, `INGESTION.md`) |
| Env / infra | **uv** (Python 3.13 venvs), **Docker** (bumped to ~14 GB), macOS |
| Extras | **PyMuPDF** (menu-PDF rasterizing), optional **OpenAI** (vision PDF parse + enrichment + query understanding); **all optional** — deterministic fallbacks, no key needed |

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
- **Multi-source ingestion** (`ingest/`, see **`INGESTION.md`**): any source → one `MenuItem` schema → shared enrichment → hybrid Vespa. Adapters: `synthetic` (curated catalog), `hf` (real **Food.com** recipes), `pdf` (menu PDFs via **vision-LLM** or text fallback). Mirrors ezCater's Temporal+Kafka→Vespa pipeline. The `dish` index now carries a `source` provenance field.
- **FastAPI proxy** (`server/main.py`): `/api/typeahead`, `/api/search` (keyword|semantic|hybrid|**understood**, + `source`/cuisine/dietary/price filters), `/api/understand`, `/api/sources` (provenance facet), `/api/health` (live LLM-policy flags).
- **React UI** (`web/`): tab switcher, keyword-vs-hybrid split, **query-understanding view** (NL → extracted concepts, side-by-side vs plain hybrid), source badges + source filter, working facet filters, LLM-policy pill.
- **Food ontology / enrichment** (`ingest/enrich.py` + `taxonomy.py` + `off.py`): ingredient→allergen/diet inference (curated spine + optional Open Food Facts + optional LLM), cached by ingredient-set hash — the "backend data enrichment" use case. Dietary inference has a safety guard (never mislabels meat as vegan).
- **Query understanding** (`server/main.py`): NL query → `{dietary, exclude_allergens, spice_min, cuisine, max_price_pp, headcount, ...}` → precise Vespa query with allergens as **hard filters** — the "frontend query understanding" use case.
- **LLM cost policy** (`ingest/config.py`): LLM is **index-time only + cached** (`LLM_INDEX=auto`); the query hot path is **deterministic by default** (`LLM_QUERY=off`). Everything runs with **no key**. Uses **OpenAI** when a key is present. See **`INTERVIEW-PREP.md`**.

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

# Run the multi-source ingestion into the dish index (synthetic + Food.com + menu PDFs):
INGEST=1 bash run.sh          # or the individual commands in INGESTION.md
```

Stop: Ctrl-C the `run.sh` (stops API + UI). `docker stop ezcater` pauses Vespa (data persists);
`docker start ezcater` resumes (~20 s). Only a full `docker rm` deletes the indexed data.

> Note: plain `bash run.sh` used to re-deploy + re-embed every time — that's fixed; it now
> skips straight to the API+UI when the Vespa container already has data.

---

## Current state (2026-07-02)

- Vespa container **up**. `dish` index fed from **three real sources** (synthetic catalog · **Food.com** recipes · menu **PDFs**) with a `source` provenance field; covid **171,332** · quora **522,931** intact.
- **Multi-source ingestion pipeline** (`ingest/`) built + validated: `MenuItem` schema, `SourceAdapter` interface, shared food-ontology enrichment, unified `run_ingest.py` CLI.
- **Ontology graph** (`ingest/graph.py`, NetworkX): real ingredient/allergen/diet/cuisine graph, seeded from the taxonomy and **LLM-grown at ingest** (136 → 560 ingredients after an LLM re-feed). Used at index time (enrich) *and* query time (concept expansion).
- **Query understanding**: **LLM-first + two-tier semantic cache** (`ingest/semcache.py`) — instant exact tier, then a **local e5-small-v2** tier (~20 ms, same model family as search, no external call). Deterministic regex fallback.
- **Serving** (FastAPI): graph-expanded hybrid (BM25 + e5 vectors + hard filters, RRF); **SSE streaming** (`/api/understand_stream`) of the LLM tokens + results in one call; per-approach timing; provenance facet; working filters. Fixed the `closeness(field,embedding)` key so the semantic score is real.
- **React UI**: **3-column progressive** compare (Keyword | Hybrid | Understood) with the understood column **streaming the LLM live**; collapsed unified understanding panel (concepts + SVG graph) + "how this search works" pipeline; keyword/graph highlighting; per-head prices; ranking labels.
- **Docs**: `INGESTION.md` (architecture + diagram + what's real/synthesized/not-built) and `INTERVIEW-PREP.md`.
- **Latency measured**: Vespa 5–37 ms; novel understood query ~2.5–4 s (LLM); exact repeat 0 ms; paraphrase ~24 ms (local e5).

## Roadmap / open decisions

1. **Bigger real corpus** — HF unauthenticated streaming is slow (~6 docs/s); set `HF_TOKEN` or cache the parquet locally to feed tens of thousands of real recipes fast.
2. **Real Redis + Open Food Facts live** — flip `REDIS_URL` + `OFF_LIVE=1` to demo the caching/corroboration story against real infra.
3. **GBDT reranker** — add a LightGBM second-phase model in Vespa (learning-to-rank on synthetic order signals) to make the ranking-ML story runnable, not just narrated.
4. **`datahive` HF profile** — richer real diet/cuisine labels (CC BY-NC) as an alternate `--dataset`.
5. **Web PDF upload** — drag-and-drop a menu in the UI → vision-LLM parse → feed Vespa → searchable (currently ingestion is CLI-only via `run_ingest --source pdf`).
6. Optional v2: geo delivery-radius, ranking contexts, real-time availability, query-time GraphRAG behind an "assistant" entry point.
