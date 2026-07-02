# ezCater ML/LLM Search Specialist — interview prep

Companion to the demo. What I built, why, and the technical areas the role probes.
The demo is the proof; this is the talk track.

---

## 0. The 60-second story

> ezCater's search already runs on **Vespa**, behind a **Temporal**-orchestrated, **Kafka**
> -fed indexing pipeline with a two-stage filter (availability → Vespa) over 125k+
> restaurants. So I built a demo that generalizes their document-transcription intake into a
> **source-agnostic ingestion platform**: any source — a real Food.com recipe corpus, a
> decorative catering **PDF parsed by a vision-LLM**, a POS row — passes through a pluggable
> adapter into one common `MenuItem` schema. A **shared enrichment stage** does the
> food-ontology work (link ingredients to a taxonomy, infer allergens/diet, embed) and
> **denormalizes** it into flat fields. **Vespa** serves it as one hybrid query — BM25 ⊕ e5
> vectors, allergens/diet as **hard filters**, RRF rerank, low-tens-of-ms. I keep the LLM
> **offline at index time where it's cached and bounded**, and the query hot path
> **deterministic** — because you can't put a per-query model in front of consumer search
> traffic.

---

## 1. Vespa — why it's the right engine, and its trade-offs

**What it is:** one engine that does lexical (BM25), vector (HNSW ANN), and structured
filtering **together**, with a programmable multi-phase ranking framework, over a
distributed content cluster with real-time writes and partial updates.

**Why ezCater picked it (and the honest case):**
- **One engine, not three.** Elasticsearch/OpenSearch (lexical) + a vector DB (Pinecone/
  Weaviate) + a filter/rules layer means three systems to keep consistent. Vespa unifies
  lexical + vector + structured + ranking, so a query like *"vegan, no nuts, under $20/head,
  near me, ranked by relevance+popularity"* is **one query, one round-trip**.
- **Ranking is first-class.** Phased ranking (cheap first-phase over many docs → expensive
  second-phase/global-phase over top-N), tensor expressions, and you can run a **GBDT/ONNX
  model inside the engine** at rank time. This is exactly what a search-ranking role needs.
- **Scale + freshness.** Proven to billions of docs; real-time partial updates (bump a
  `popularity` or `availability` attribute without reindexing the doc).

**Trade-offs (say these — they show maturity):**
- **Ops/learning curve.** `services.xml` + schema + ranking is more to learn than ES; smaller
  community; fewer managed options (Vespa Cloud exists).
- **Overkill for simple search.** If you only need lexical, ES is simpler; if you only need
  vectors, pgvector/Pinecone is simpler. Vespa wins when you need **all three + ranking at
  scale** — which is ezCater.
- **Memory-bound.** Attributes + HNSW live in memory; capacity planning matters.

**Ranking specifics I can speak to:** `first-phase`/`second-phase`/`global-phase`,
`rerank-count`, `closeness(field, embedding)` for angular distance, `reciprocal_rank_fusion`
for hybrid, `match-features`/`summary-features` for debugging, `nearestNeighbor` with
`targetHits`, grouping/aggregation, streaming search for high-cardinality personal corpora.

---

## 2. Retrieval quality — keyword vs vector vs hybrid

- **BM25** = exact term match; great for head/navigational queries, blind to synonyms.
- **Embeddings (e5-small-v2, 384-d, int8, in-engine)** = semantic; finds "plant-based" for
  "vegan", but can drift on rare tokens/entities.
- **Hybrid = both, fused.** I use **reciprocal rank fusion** in a global phase. In my
  capstone on trec-covid I measured **nDCG@10 ≈ 0.74 (hybrid) vs 0.59 (semantic) vs 0.51
  (keyword)** — hybrid wins because the two signals fail on different queries.
- **How I'd improve retrieval further:** (1) a **cross-encoder / LTR reranker** on the top-N
  (see §4); (2) **two-tower** models fine-tuned on ezCater click/order data so the query and
  menu-item towers share a catering-specific space; (3) better fields (synonyms, learned
  sparse like SPLADE); (4) query understanding to add hard constraints (see §3).

---

## 3. LLMs in search — the two use cases (this is the role)

**(a) Backend data enrichment (index-time).** The tag-accuracy problem: 125k caterers
hand-tag menus, so `vegan`/`gluten-free`/`spicy` are inconsistent and sparse. Fix: at
**index time**, extract ingredients from menu text and **infer** allergens/diet from a food
ontology (+ optional LLM), then denormalize. Offline, once per item, **cached** → bounded
cost. This is `ingest/enrich.py` in the demo.

**(b) Frontend query understanding.** NL query → structured concepts → precise query.
*"spicy vegan lunch for 15 under $20 a head"* → `{dietary:[vegan], spice_min:2,
max_price_pp:20, headcount:15}` → Vespa filters `dietary contains "vegan" AND spice_level>=2
AND price_pp<20`, plus semantic match on the residual intent. This is `/api/understand` +
`mode=understood`.

**The cost decision (I lead with this):** query understanding is **deterministic by default**.
An LLM per query = cost × QPS + latency + prompt-injection from untrusted input. So the LLM
is index-time (cached) for enrichment, and an **opt-in toggle** for query understanding — not
the default hot path. `config.py` encodes it: `LLM_INDEX=auto`, `LLM_QUERY=off`.

**Latency/caching for any LLM path:** (1) enrichment cache keyed by ingredient-set hash;
(2) result cache (hot query → results); (3) **semantic cache** keyed on query embedding so
paraphrases reuse a prior expensive answer. Redis for all three.

---

## 4. Ranking ML — GBDT / learning-to-rank

- **Where it sits:** retrieval (BM25⊕vector, cheap, high-recall) narrows to top-N; a
  **LightGBM/GBDT reranker** (LambdaMART, pairwise/listwise) reorders the top-N with rich
  features. In Vespa this runs as a **second-phase/global-phase** model (ONNX/GBDT) inside
  the engine.
- **Features:** BM25 & vector scores, price/serves, caterer rating & popularity, dietary
  match, distance/ETA, historical CTR/conversion, freshness, personalization.
- **Training signal:** clicks + **orders** (order is the money label). Learning-to-rank on
  logged interactions.
- **Position bias** is the trap: top results get clicked because they're on top, not because
  they're best. Correct with (1) **randomization / interleaving**, (2) **inverse-propensity
  weighting**, (3) a position feature at train time zeroed at serve time. Evaluate with
  **A/B tests** on business metrics (conversion, GMV, add-to-cart), not just offline nDCG.

---

## 5. Food ontology / knowledge graph — build & trade-offs

- **Why a KG:** relationships — *ingredient → allergen*, *dish → cuisine*, *"healthy" →
  which attributes* — power both enrichment (infer missing tags) and reasoning (multi-hop
  planning).
- **How to build:** seed a curated taxonomy (**FoodOn**, **Open Food Facts** allergen tags)
  as the reliable spine; extend with **LLM extraction** (LangChain `LLMGraphTransformer`,
  LlamaIndex KG index, or REBEL) on menu text; store in **Neo4j/Neptune** (property graph) or
  **RDF/OWL** (GraphDB/Stardog) if you want formal reasoning/SPARQL.
- **KG + Vespa (the pragmatic answer):** do the graph inference **offline at index time** and
  **denormalize** the flat result into Vespa attributes → fast hybrid serving. Reserve
  **GraphRAG** (Microsoft; local/DRIFT traversal + LLM) for the narrow slice of conversational
  multi-hop queries — it's ~2.3× latency and 10×+ indexing tokens, not for head traffic.
  Vespa parent/child references cover one narrow case (small shared dimension); Vespa is not a
  graph engine — don't traverse in it.

---

## 6. Likely questions → crisp answers

- **"Why Vespa over Elasticsearch + a vector DB?"** → §1: one engine for lexical+vector+
  structured+ranking at scale = one query, one system to keep consistent, ranking first-class.
- **"How would you improve tag accuracy for 125k caterers?"** → §3a: index-time enrichment
  (ontology + optional LLM), denormalized, cached; declared tags trusted, inferred tags
  guarded (never mislabel meat as vegan); human review on low-confidence items.
- **"Would you use an LLM to understand every query?"** → §3: no — cost/latency/injection at
  their QPS. Deterministic hot path; LLM index-time (cached) + opt-in for query understanding.
- **"How do you rank results?"** → §4: retrieval → GBDT reranker in Vespa's second phase on
  order-weighted LTR; watch position bias; validate with A/B on conversion.
- **"How do you evaluate?"** → offline nDCG/MRR/recall for iteration; **online A/B** on GMV/
  conversion for decisions; guard against position bias.
- **"Onboard a caterer's PDF menu?"** → §demo: vision-LLM → strict JSON schema, never invent
  price/allergen, null on missing, per-item confidence, low-confidence → human review. Exactly
  what they do by hand today.
- **"Cold start / new caterer?"** → popularity priors by cuisine/zone, content features
  (embeddings work with zero interactions), explore/exploit.

---

## 7. What the demo demonstrates (map to the JD)

| JD signal | In the demo |
|---|---|
| Vespa fluency | 3 indexes, hybrid + RRF, gram typeahead, phased ranking, additive deploys, hard-filter YQL |
| LLM data enrichment | `ingest/enrich.py` — ingredient→allergen/diet, cached, index-time |
| Query understanding | `/api/understand` + `mode=understood`, deterministic-default |
| Food ontology / KG | `taxonomy.py` spine + Open Food Facts corroboration + denormalization |
| Ingestion pipelines | pluggable `SourceAdapter`s → one schema (mirrors Temporal+Kafka→Vespa) |
| Cost/latency judgment | the LLM policy (`config.py`), enrichment cache, hard filters over LLM |
| Retrieval metrics | capstone nDCG 0.74/0.59/0.51; scale-tested to 522,931 docs locally |
