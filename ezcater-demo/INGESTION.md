# Multi-source ingestion → food ontology → hybrid Vespa

A source-agnostic ingestion pipeline that mirrors ezCater's real architecture:
**any source → one common `MenuItem` schema → one shared enrichment stage → one hybrid
Vespa index.** Source-specific messiness lives only in adapters; everything downstream is
uniform.

> Grounded in ezCater's verified stack: their production search runs on **Vespa**, fed by a
> **Temporal-orchestrated** indexing pipeline ("ingest, validate, transform, write") over
> **Kafka**, with a two-stage filter (zone/availability → Vespa filtering + ranking) across
> 125k+ restaurants. This demo generalizes their document-transcription intake into an
> automated multi-source parser and reproduces that Temporal+Kafka→Vespa spine in miniature.

---

## The picture

```
 SOURCES                 ADAPTERS                 COMMON         ENRICHMENT (shared, index-time)      SERVE
 (heterogeneous)      (normalize→MenuItem)        SCHEMA                                          (hybrid Vespa)
┌─────────────────┐  ┌──────────────────┐
│ Menu PDFs/images│─▶│ pdf_adapter      │─┐      ┌───────────────────────────────────────────┐
│  (branded/scan) │  │  vision-LLM/text │ │      │  1. taxonomy  ingredient→allergen/diet     │
├─────────────────┤  ├──────────────────┤ │      │     (curated FoodOn-style spine)           │
│ HuggingFace sets│─▶│ hf_adapter       │ ├────▶ │  2. Open Food Facts  live corroboration    │──┐
│  (Food.com …)   │  │  column/parquet  │ │ List │     (optional, OFF_LIVE=1)                 │  │
├─────────────────┤  ├──────────────────┤ │[Menu │  3. LLM extract  ingredients/allergens     │  │
│ (stub) Databases│─▶│ db_adapter       │ │ Item]│     (optional, index-time, CACHED)         │  │
├─────────────────┤  ├──────────────────┤ │      │  4. infer dietary + safety guard           │  │
│ (stub) Web/API  │─▶│ web_adapter      │─┘      │  5. Vespa embeds e5 vector at feed time    │  │
└─────────────────┘  └──────────────────┘        │  6. DENORMALIZE all labels into flat fields│  │
                                                 └───────────────────────────────────────────┘  │
                                                          ▲ memoized by ingredient-set hash      │
                                                   ┌──────┴───────┐                              ▼
                                                   │ enrich cache │            ┌──────────────────────────────┐
                                                   │ (disk/Redis) │            │  VESPA  (one engine)          │
                                                   └──────────────┘            │  BM25 ⊕ nearestNeighbor(e5)   │
                                                                               │  + HARD filters: allergens,   │
                                                                               │    dietary, spice, price      │
                                                                               │  + RRF global-phase rerank    │
                                                                               └──────────────────────────────┘
```

### How each block maps to ezCater's production stack

| This repo | ezCater production equivalent |
|---|---|
| Source adapters emit normalized records | Menu API / POS middleware / transcription output on **Kafka** topics |
| Shared enrichment stage | one **Temporal workflow**; each step (link → extract → infer → embed → denormalize) is a retryable, idempotent activity |
| Enrichment cache (`ingest/cache.py`) | memoization so re-indexing / Kafka replays don't re-hit LLMs or diet APIs |
| Vespa hybrid index + hard filters | the **Vespa** engine; `allergens`/`dietary` filters mirror the two-stage filter |

One-liner: **adapters are Kafka producers; enrichment is one Temporal workflow; Vespa is the sink.**

---

## The contract: one schema, one interface

`ingest/menu_item.py` defines the whole contract.

```python
@dataclass
class MenuItem:
    id; name; description; cuisine; course; serves; price; price_pp; caterer_name; source
    ingredients_raw: list[str]      # what the source stated
    dietary_declared: list[str]     # tags the source stated
    # --- enrichment fills these (denormalized KG output) ---
    ingredients; allergens; dietary; spice_level; flavor; occasion
    confidence: float               # <1.0 => route to human review (PDF/LLM sources)

class SourceAdapter(Protocol):
    name: str
    def fetch(self, **opts) -> Iterator[object]: ...        # pull raw records
    def to_menu_items(self, rec) -> list[MenuItem]: ...      # normalize one record
```

Adding a source = writing one adapter. Enrichment and serving never change.
Adapters live in `ingest/adapters/` and register in `REGISTRY`.

| Adapter | Source | Notes |
|---|---|---|
| `synthetic` | `data/dishes.jsonl` | the original curated catalog, replayed through the same pipeline |
| `hf` | HuggingFace recipes | `foodcom` (MIT, ~1M rows) or `datahive` (real diet/cuisine labels). Streams a shard; synthesizes catering price/caterer deterministically |
| `pdf` | menu PDFs in `menus/` | **vision-LLM** extraction when a key is present; deterministic text-parse fallback otherwise |
| `db` / `web` | — | interface stubs (extensibility shown, not built) |

---

## Enrichment: the food-ontology "backend data enrichment" use case

`ingest/enrich.py` fills the fields Vespa filters on. Three layers, most-to-least trusted:

1. **`taxonomy.py`** — a curated ingredient → {allergens, animal-class} map (a small,
   auditable FoodOn-style spine). Always runs. Word-boundary + plural-aware matching so
   `eggplant` never triggers `egg` and `cashews` still maps to `nuts`.
2. **Open Food Facts** (`off.py`) — optional live corroboration (`OFF_LIVE=1`), cached.
3. **LLM** (`llm.py`) — optional, index-time only, memoized by ingredient-set hash.

Then: **dietary inference with a safety guard** — inferred (not source-declared)
`vegan`/`vegetarian` is *blocked* when the name/description names an animal (so "Chili Con
Carne" from a messy recipe corpus is never mislabeled vegan). **Allergen exclusion is
always a hard filter, never a ranking signal.**

---

## The LLM cost policy (the important design decision)

An LLM in front of consumer search traffic is a business risk — per-query cost × QPS ×
125k caterers, plus latency and prompt-injection from untrusted input. So the LLM is used
**only where its cost is bounded**:

| | Default | Where | Cost profile |
|---|---|---|---|
| **Index-time LLM** (`LLM_INDEX`) | ON iff a key exists | PDF parsing, enrichment | offline, once per item, **cached** — bounded/amortized |
| **Query-time LLM** (`LLM_QUERY`) | **OFF even with a key** | query understanding | the hot path stays a **deterministic parser**; LLM is an opt-in toggle to *demo* the capability |

Everything runs end-to-end with **no key and no external calls**. `config.py` is the single
source of truth; the UI's header pill and `/api/health` surface the live policy.

Interview line: *"I used the LLM exactly where its cost is bounded — offline and cached —
and kept the query hot path deterministic, because you can't risk a per-query model in
front of consumer search traffic."*

---

## KG + Vespa: index-time by default, GraphRAG only when it earns it

* **Default (fast):** the graph does its expensive work offline (link → infer allergen/diet),
  and *flattens the answers* into plain Vespa attributes. Query time = one BM25+ANN hybrid
  with hard filters, low-tens-of-ms, no per-query graph traversal or LLM. This is what the
  demo does.
* **Advanced (opt-in, narrow):** query-time GraphRAG only for conversational multi-hop
  ("plan a nut-free vegan dinner for 12 across these caterers"). Honest cost: ~2.3× latency,
  10×+ indexing tokens — never for the ~95% head queries.
* **Redis** highest-value layer = the enrichment cache (memoize by ingredient-set hash).
  Result cache and semantic cache are the other two layers.

Keep the rich, cyclic graph in Neo4j/Neptune for *inference*; store only the flat *result*
in Vespa. Vespa parent/child references fit one narrow case (a small, shared, frequently
-updated dimension like an `ingredient` node), but Vespa is not a graph engine — don't
traverse in it.

---

## What is REAL vs SYNTHESIZED vs NOT-BUILT (full honesty)

| Thing | Status |
|---|---|
| Food.com recipe names, descriptions, ingredient lists | **real** (HuggingFace `AkashPS11/recipes_data_food.com`, MIT) |
| Menu-PDF item names/prices/descriptions | **real** (transcribed from the actual PDF in `menus/`) |
| trec-covid / quora corpora | **real** |
| Vespa hybrid search, RRF, HNSW, gram typeahead, hard filters | **real**, running |
| Ingredient → allergen/diet enrichment | **real logic** via the **ontology graph** (`graph.py`), seeded from the curated taxonomy and **grown by the LLM** at ingest for unknown ingredients (cached) |
| **Ontology graph** (ingredient/allergen/diet/cuisine nodes + edges) | **BUILT** — a real NetworkX graph (`graph.py`), seeded from the taxonomy, LLM-grown, persisted, used at **index time** (enrich) *and* **query time** (concept expansion). It is a lightweight in-process graph, **not** a Neo4j/RDF database. |
| Query understanding | **LLM-first** (OpenAI) with a **semantic cache** (`semcache.py`) keyed on the query's intent embedding — paraphrases reuse one LLM answer. Deterministic regex parser is the fallback (`LLM_QUERY=off`). |
| Query-time hybrid | **keyword (BM25) + embedding (e5, graph-expanded intent) + hard filters**, fused by RRF in Vespa. |
| **Prices & caterer names on Food.com recipes** | **SYNTHESIZED** — recipes ship no catering price, so `hf.py` fabricates a deterministic per-head price ($8–$30) and a caterer name. Clearly fake; swap for real data if available. |
| LLM enrichment on the recipe corpus | **NOT applied to the bulk** — the 1,228 recipes were fed with `LLM_INDEX=off` (deterministic + graph). The LLM ran on the **12 PDF items** (vision) and grows the graph on-demand. Re-run with `LLM_INDEX=on` to LLM-enrich recipes too. |
| **Neo4j/RDF graph database + GraphRAG** (query-time multi-hop traversal) | **NOT BUILT.** The ontology graph above is in-process and used for enrichment + expansion, not multi-hop LLM traversal. GraphRAG remains the described "production evolution." |

## Run it

```bash
# from ezcater-demo/, using the capstone venv
PY=../capstone/.venv/bin/python

# inspect only — enrich 5 real recipes and print them (no Vespa, no LLM spend)
LLM_INDEX=off $PY -m ingest.run_ingest --source hf --limit 5 --print

# one-time: additive redeploy (adds `source` field) + feed the sample catalog
$PY -m ingest.run_ingest --source synthetic --deploy

# feed real recipes (deterministic enrichment, bulk)
LLM_INDEX=off $PY -m ingest.run_ingest --source hf --dataset foodcom --limit 5000

# ingest menu PDFs from ./menus  (vision-LLM if OPENAI key present, else text-parse)
$PY -m ingest.run_ingest --source pdf

# generate the sample menu PDF fixture first, if needed
$PY menus/make_sample_menu.py
```

Toggles: `LLM_INDEX=off|on|auto`, `LLM_QUERY=on` (default off), `OFF_LIVE=1`
(Open Food Facts), `REDIS_URL=...` (real Redis instead of disk cache),
`OPENAI_MODEL` / `OPENAI_VISION_MODEL`.

Files: `ingest/menu_item.py` (contract) · `ingest/adapters/*` (sources) ·
`ingest/enrich.py` + `taxonomy.py` + `off.py` (enrichment) · `ingest/llm.py` +
`config.py` (LLM policy) · `ingest/cache.py` (memoization) · `ingest/run_ingest.py` (CLI).
