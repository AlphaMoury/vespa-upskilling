# Vespa.ai — A Deep Dive You Can Read in an Afternoon

> Goal: after reading this once, you can (a) explain what Vespa is and why it exists, (b) read and write a schema and a ranking profile, (c) reason about how a query is executed, and (d) hold your own in a technical conversation about when to choose Vespa. The capstone then turns this into muscle memory.

Read top-to-bottom. Each major section opens with a one-sentence summary so you can skim. Terms in **bold** are the official Vespa vocabulary — learn to say them precisely; that's half of sounding fluent.

---

## 1. What Vespa is, and the problem it solves

**In one sentence:** Vespa is an open-source "big data serving engine" that combines storage, full-text search, vector search, structured filtering, and machine-learned ranking into a single system that computes rankings *next to the data*.

### The pain it removes

A typical modern search/recommendation/RAG stack is **three systems duct-taped together**:

1. A **keyword search engine** (Elasticsearch / OpenSearch / Solr) for exact-term matching, BM25, filters.
2. A **vector database** (Pinecone / Weaviate / Qdrant / Milvus) for semantic / embedding similarity.
3. A **ranking / inference service** (a model server) that re-scores candidates with an ML model.

Plus glue code to fan out to all three, merge results, and re-rank. That architecture has four chronic problems:

- **Two copies of your data**, two indexes to keep consistent.
- **Ranking happens far from the data**: you retrieve candidates, ship them across the network to a model server, score, and re-sort — which adds latency and caps how many candidates you can realistically rank.
- **Freshness gaps**: engines like Elasticsearch index into immutable segments, so updates aren't instantly searchable.
- **Operational sprawl**: three systems to scale, monitor, secure, and synchronize.

**Vespa's thesis:** put retrieval + ranking + ML inference *on the same nodes that store the documents*. Then lexical signals, vector similarity, structured attributes, and ML models are all evaluated in a **single query**, and the expensive scoring runs **where the data already lives** — no shipping candidates around. This is the phrase to remember: **"compute close to the data."**

### Where it came from (the credibility story)

- Roots ~2001–2004 in the **AlltheWeb / FAST** search lineage (Trondheim, Norway); **acquired by Yahoo in 2003**.
- Ran inside Yahoo for ~20 years powering search, ads, and content recommendation — at the scale of **~150 applications, ~800,000 queries/second, ~1 billion users**.
- **Open-sourced in 2017** (Apache 2.0).
- **Spun out as the independent company Vespa.ai in October 2023** (CEO Jon Bratseth, a longtime Yahoo architect); raised a $31M Series A shortly after.

So when someone asks "is this a toy?": it's the opposite — it's 20-year-old battle-tested infrastructure that was recently freed from one company. Today it powers, among others, **Perplexity's** retrieval layer, **Spotify** podcast/semantic search, and **Vinted's** marketplace search (more in [the slides brief](#11-who-uses-it-and-where-it-fits)).

---

## 2. The mental model: how Vespa is put together

**In one sentence:** You write a declarative **application package** describing your data (**schema**) and how to score it (**rank profiles**); you deploy it to a cluster split into stateless **container** nodes (handle requests) and stateful **content** nodes (store + search + rank); you feed JSON documents and query with **YQL**.

```
                      ┌──────────────────────────────────────────────┐
   query / feed  ───► │  CONTAINER cluster (stateless, JVM "jdisc")   │
   (HTTP, :8080)      │  - parses queries, runs Searchers/chains      │
                      │  - runs embedders, doc processors             │
                      │  - global-phase re-ranking, result rendering  │
                      └───────────────┬──────────────────────────────┘
                                      │ fan-out
                      ┌───────────────▼──────────────────────────────┐
                      │  CONTENT cluster (stateful)                   │
                      │  each node = distributor + "proton" backend   │
                      │  - stores documents in buckets                │
                      │  - matches (BM25 + ANN/HNSW + filters)        │
                      │  - first-phase & second-phase ranking HERE    │
                      └──────────────────────────────────────────────┘
```

Two cluster types, and the split is the whole point:

- **Container cluster** (a.k.a. *jdisc*, the JVM container): the **stateless** request layer. It accepts queries and feeds, runs **searcher** chains and **document processors**, generates embeddings, does the final **global-phase** re-ranking across merged results, and renders responses. You scale it for request volume / CPU.
- **Content cluster**: the **stateful** layer. Each content node stores a slice of the documents and runs the **matching** and **first/second-phase ranking** locally, then returns its top hits to the container to be merged. You scale it for data volume and ranking cost. The two processes on a content node are the **distributor** (decides which node/bucket a document belongs to and keeps replicas healthy) and **proton** (the C++ search core that actually indexes, matches, and ranks).

Supporting infrastructure you'll see named but rarely touch: a **config server / config cluster** (ZooKeeper-backed; receives your deployed application package), a **cluster-controller** (tracks node health, drives the cluster to its "ideal state"), a **logserver**, and **slobroks** (service naming). On a laptop, the single `vespaengine/vespa` Docker container runs all of these.

### The application package — the unit of deployment

You never click around an admin UI to configure Vespa. **The only way to change a running Vespa is to edit an application package and redeploy it.** That package is just a directory:

```
my-app/
├── services.xml                 # which clusters exist, how many nodes, which components
├── schemas/
│   └── doc.sd                    # one .sd file per document type (the data model + ranking)
├── components/                   # optional: custom Java (searchers, doc processors) as .jar
├── models/                       # optional: ONNX / XGBoost / LightGBM model files
└── search/query-profiles/        # optional: named bundles of default query parameters
```

- **`services.xml`** is the topology: it declares the `<container>` and `<content>` clusters, how many nodes each has, redundancy, and which **components** (e.g. embedders, custom searchers) are loaded. A minimal app can be almost empty; a production one is detailed.
- **`schemas/*.sd`** is where you spend most of your time. One file per **document type**.

> **pyvespa shortcut:** the Python library builds this package *for you* from Python objects (`ApplicationPackage`, `Schema`, `Field`, `RankProfile`, `Component`). You don't have to hand-write XML to start. The capstone uses this. But you should be able to *read* the `.sd`/XML form — peek at it with `package.to_files()` or in `app/` after deploy.

---

## 3. The schema — modeling your data

**In one sentence:** A schema (`.sd` file) defines a **document type**, its **fields** and **types**, how each field is **indexed** (`index` / `attribute` / `summary`), and the **rank profiles** used to score it.

Skeleton of a schema:

```
schema doc {
    document doc {
        field title type string {
            indexing: summary | index
            index: enable-bm25
        }
        field body type string {
            indexing: summary | index
            index: enable-bm25
        }
        field popularity type int {
            indexing: summary | attribute
        }
        field embedding type tensor<float>(x[384]) {
            indexing: input title . " " . input body | embed e5 | attribute | index
            attribute { distance-metric: angular }
            index { hnsw { max-links-per-node: 16  neighbors-to-explore-at-insert: 200 } }
        }
    }
    fieldset default { fields: title, body }

    rank-profile bm25 {
        first-phase { expression: bm25(title) + bm25(body) }
    }
}
```

### Field types

Scalars: **string, int (32-bit), long (64-bit), float, double, byte, bool, position, predicate** (boolean constraints), **raw** (binary). Collections: **array\<T\>**, **map\<K,V\>**, **weightedset\<T\>** (each element carries an integer weight — great for tags/categories with scores), **struct**, and **reference\<doctype\>** (a foreign key for parent/child relationships). And the star of the show: **tensor** (see §5).

### The three indexing modes — the single most important schema concept

The `indexing:` statement is a pipeline (read the `|` as "and also"). Each field can be sent to one or more of three destinations:

| Mode | What it builds | Enables | Cost |
|------|----------------|---------|------|
| **`index`** | Tokenized, stemmed **inverted index** | Full-text match, **BM25**; on a tensor field, the **HNSW** vector index | Disk-backed, supports huge fields |
| **`attribute`** | In-memory **columnar** store | Fast **filtering, sorting, grouping, ranking access**; required for tensors used in ranking / ANN | Held in memory (RAM cost) |
| **`summary`** | Marks field **returnable** in results | Field shows up in hits | Cheap |

So `indexing: summary | index` = searchable full-text *and* returned in results. `indexing: summary | attribute` = filter/sort/rank on it *and* return it, but not full-text searchable. Getting these right is most of schema design.

Other indexing operations you'll meet: `input` (read a source field), `embed` (run an embedder — see §6), `tokenize`, `normalize`, `lowercase`, `set_language`, `for_each`, and string concatenation with `.`.

A **fieldset** (`fieldset default { fields: title, body }`) groups fields so a user query searches them together. `userQuery()` searches the `default` fieldset unless told otherwise.

---

## 4. Documents: feeding, updating, real-time

**In one sentence:** Documents are JSON, addressed by a structured **document id**, written through the Document API; **partial updates to attributes are in-memory and instantly searchable** — a genuine differentiator.

- **Document id** format: `id:<namespace>:<doctype>::<your-id>` — e.g. `id:tutorial:doc::MED-123`.
- **Feed** via the high-throughput `vespa-feed-client` (HTTP/2, async, auto-retry, dynamic throttling) or, in Python, `app.feed_iterable(...)`. A document is just `{"id": "...", "fields": {...}}`.
- **Partial updates**: you can update a single attribute field without re-feeding the whole document. Because attributes are in-memory, this is a fast in-place write (no read-modify-write, no segment merge) and the change is **searchable immediately**. Update operations include `assign` (replace), `add` / `remove` (for arrays/weightedsets/tensors), and arithmetic `increment` (e.g. bump a click counter live and have it affect ranking on the very next query).
- **Test-and-set**: writes can be guarded by a condition for safe concurrent updates.

This is why Vespa is strong for **recommendation and personalization**: you can stream behavioural signals (clicks, stock levels, prices) into attributes in real time and have them change ranking instantly, without reindexing.

---

## 5. Tensors — Vespa's secret weapon

**In one sentence:** Tensors are Vespa's first-class multi-dimensional data type; they're how embeddings, model weights, and ML computations are represented and combined inside the engine.

A tensor has named dimensions, each either **indexed** (dense, integer indices, fixed size) or **mapped** (sparse, string labels). Notation:

- `tensor<float>(x[384])` — a **dense** 384-d vector (a typical embedding).
- `tensor<float>(token{})` — a **sparse** map from token → weight (e.g. a SPLADE lexical vector).
- `tensor<float>(token{}, x[128])` — **mixed**: one 128-d vector per token (e.g. ColBERT late-interaction).

Cell types trade precision for memory: `float`, `double`, and crucially `bfloat16` (half the memory, common for embeddings) and `int8` (binary/quantized vectors — 8× smaller, used in the RAG Blueprint).

Why it matters: because embeddings and model weights are *the same type*, a **ranking expression can do real linear algebra** — `reduce`, `join`, `map`, `matmul` — to compute similarities or run a small model, right on the content node. Tensors are the substrate that lets "search" and "ML inference" be the same operation.

---

## 6. Embeddings & vector search

**In one sentence:** Vespa can generate embeddings *itself* (built-in embedders), index them with **HNSW** for approximate nearest-neighbor search, and you query them with the `nearestNeighbor` operator.

### Built-in embedders (so you don't run a separate model server)

Declared as `<component>`s in the application. The main ones:

- **`hugging-face-embedder`** — the recommended general-purpose one; runs most HuggingFace models exported to **ONNX** with their tokenizer. (The capstone uses this with **e5-small-v2**, 384-d.)
- **`colbert-embedder`** — late-interaction: one vector per token, with strong compression. Best quality, more storage.
- **`splade-embedder`** — learned **sparse** lexical vectors (a mapped tensor of token→weight). Added in Vespa 8.321.
- **`bert-embedder`** — older, now deprecated in favor of the HF embedder.
- API-based options too: **OpenAI, Cohere, VoyageAI, Mistral**.

The magic move: the same `embed` step runs at **index time** (to embed documents) and at **query time** (to embed the query), so you feed and search with *text* and never touch a vector yourself:

```
# document side, in the schema:
field embedding type tensor<float>(x[384]) {
    indexing: input title . " " . input body | embed e5 | attribute | index
    attribute { distance-metric: angular }
    index { hnsw {} }
}
# query side, in the request:  input.query(q) = embed(e5, "the user's question")
```

### HNSW and the `nearestNeighbor` operator

Adding `index { hnsw {} }` to a tensor attribute builds a **Hierarchical Navigable Small World** graph for **approximate** nearest-neighbor (ANN) search — sub-linear, scales to billions of vectors. The `distance-metric` matters and must match your model: `angular` (cosine-like, for normalized embeddings like e5), `euclidean` (default), `dotproduct`, `prenormalized-angular`, `hamming` (for binary/int8 vectors).

In YQL you retrieve with `{targetHits:100}nearestNeighbor(embedding, q)` — "give me ~100 nearest documents in field `embedding` to query tensor `q`". For ranking you use the **`closeness(field, embedding)`** rank feature, which returns a 0–1 similarity (1 = identical). Without an HNSW index you still get *exact* brute-force NN — correct, just slower; fine for a few thousand docs.

---

## 7. Ranking — the heart of Vespa

**In one sentence:** Ranking is **phased** — a cheap `first-phase` runs on every matched document on the content node, an expensive `second-phase` re-ranks the top-K locally, and an optional `global-phase` re-ranks the merged top results in the container — and you express each phase as a math expression over **rank features**.

### Why phases exist

You might match a million documents but only want to run an expensive ML model on the best ~100. Phasing makes that explicit and efficient:

```
rank-profile hybrid inherits default {
    inputs {
        query(q) tensor<float>(x[384])      # the query embedding, supplied per request
    }
    function bm25sum() { expression: bm25(title) + bm25(body) }

    first-phase  { expression: bm25sum + 5 * closeness(field, embedding) }   # cheap, every match
    second-phase { expression: xgboost("ltr_model.json")  rerank-count: 100 } # ML, top 100 per node
    global-phase {
        expression: reciprocal_rank_fusion(bm25sum, closeness(field, embedding))
        rerank-count: 1000                                                    # across merged hits
    }
    match-features { bm25(title) closeness(field, embedding) }               # returned per hit (debug)
}
```

- **`first-phase`** runs on the content node for *every* matched doc — keep it cheap.
- **`second-phase`** re-ranks only the top `rerank-count` (default 100) per node — this is where you put an XGBoost/LightGBM/ONNX model.
- **`global-phase`** runs in the container on the merged top hits from all nodes — ideal for cross-node fusion (like reciprocal rank fusion) or a final ONNX cross-encoder.
- **`match-features`** / `summary-features` return chosen feature values per hit, so you can *see why* something ranked where it did. Invaluable for debugging relevance.

### Rank features (the vocabulary of scoring)

You compose expressions from built-in **rank features**:

- **Text:** `bm25(field)` (fast, the default choice; requires `index: enable-bm25`), `nativeRank`, `fieldMatch(field)` (most accurate, most expensive, with sub-features like `.proximity`).
- **Vector:** `closeness(field, embedding)` (0–1 similarity), `distance(field, embedding)` (raw).
- **Attributes / freshness:** `attribute(name)`, `freshness(timestamp)`, plus arbitrary math (`+ - * /`, `if`, `log`, tensor ops).
- **ML models:** `xgboost("file.json")`, `lightgbm(...)`, `onnx(model)` — the model file lives in the package's `models/` dir.

This is the part to internalize: **in Vespa, "relevance" is a math expression you control**, mixing lexical, semantic, structured, behavioural, and ML signals — and it runs at scale next to the data. That's the capability the three-system stack can't match cleanly.

---

## 8. Hybrid search — the pattern that sells Vespa

**In one sentence:** Hybrid search runs lexical (BM25) and vector (ANN) retrieval in *one query* and fuses their signals in the rank profile — and it reliably beats either method alone.

Pure keyword search misses synonyms and meaning ("heart attack" vs "myocardial infarction"). Pure vector search misses exact terms, names, SKUs, and rare words. **Hybrid gets both.** The mechanics:

1. **Match with either**: in YQL, combine the two retrieval operators so a document matching *either* one becomes a candidate:
   ```
   select * from doc where
     userQuery() or ({targetHits:100}nearestNeighbor(embedding, q))
   ```
   (Or use the **`rank(...)`** operator, where the first argument decides matching and the rest only contribute rank features.)
2. **Fuse in ranking**: combine `bm25(...)` and `closeness(...)`. Common strategies:
   - **Linear**: `0.5 * normalized_bm25 + 0.5 * closeness(...)` — simple, but BM25 is unbounded so you normalize it (e.g. with `atan`).
   - **Reciprocal Rank Fusion (RRF)**: `reciprocal_rank_fusion(bm25sum, closeness(...))` in the **global-phase** — uses only the *rank position* from each method, so it sidesteps the scale-mismatch problem entirely. This is what the capstone uses.

In the official tutorial on the NFCorpus dataset, hybrid (`hybrid-linear-normalize`) scored **nDCG@10 = 0.342**, beating BM25-only (0.321) and dense-only (0.308). **Your capstone reproduces exactly this result** — being able to say "I measured it" is the strongest thing you can bring to the presentation.

---

## 9. Querying with YQL

**In one sentence:** YQL is a SQL-flavored query language; you select from sources, filter with `where` (including text `contains`, `range`, and the `nearestNeighbor`/`rank` operators), and pick a rank profile with the `ranking` parameter.

```
select * from sources * where
  rank(({targetHits:100}nearestNeighbor(embedding, q)), userQuery())
  and range(popularity, 10, 1000)
  order by relevance desc
  limit 10
  timeout 5000
```

Key pieces:
- **`userQuery()`** — injects the end-user's parsed text query (BM25 over the `default` fieldset).
- **`nearestNeighbor(field, q)`** — ANN retrieval; `q` is a query tensor you pass in the request body as `input.query(q)`.
- **`rank(a, b, ...)`** — only `a` decides *whether* a doc matches; `b, c…` are evaluated purely to feed rank features. Perfect for "retrieve by vector, but also compute BM25 for ranking."
- **`contains`, `phrase(...)`, `range(f, lo, hi)`, `and/or/!`** — filters and boolean logic.
- The **`ranking`** request parameter selects which rank profile to use; **`input.query(...)`** supplies query tensors; **`hits`/`offset`/`timeout`** control paging and latency budgets.

Vespa also has powerful **grouping / aggregation** (faceting, like SQL `GROUP BY`):
```
select * from doc where true |
  all( group(category) max(10) order(-count()) each( output(count(), avg(price)) ) )
```
and **streaming search** mode (no inverted index; stream-scan a user's own small data slice — ideal for per-user mailboxes / documents).

---

## 10. RAG with Vespa (retrieval-augmented generation)

**In one sentence:** Vespa can do the *generation* step too — a built-in `RAGSearcher` retrieves with hybrid+ML ranking, builds a prompt from the hits, calls an LLM (external API or a local/GPU model), and streams the answer — so retrieval and generation live in one system.

The architecture Vespa recommends (and what powers Perplexity-style products):

```
user question
   │
   ├─ embed query  ──►  hybrid retrieval (BM25 + nearestNeighbor)
   │                       │
   │                    multi-phase ranking (first-phase → GBDT second-phase)
   │                       │
   │                    top-k chunks
   │                       ▼
   └────────────────►  RAGSearcher: build prompt with retrieved context
                          │
                       LLM client (OpenAI-compatible API, or local/GPU LLM)
                          │
                       stream answer (Server-Sent Events), with citations
```

- **LLM clients are components** in `services.xml`: `ai.vespa.llm.clients.OpenAI` (any OpenAI-compatible endpoint — OpenAI, Anthropic, Gemini, Together, etc.) or a `LocalLLM` running on-box (GPU available on Vespa Cloud).
- **Chunking** happens in the indexing language (e.g. fixed-length chunks), and you can store **multiple vectors per document**.
- **Document enrichment**: a `generate` indexing expression can even call an LLM *at ingest time* to fill fields (summaries, tags), keeping that cost out of the query path.
- The reference app to study is **`rag-blueprint`** (the "same architecture that powers Perplexity") — ~190 lexical+semantic features feeding a LightGBM ranker, then generation.

For the capstone, RAG is the **stretch goal**: get hybrid search rock-solid first, then optionally bolt on generation.

---

## 11. Who uses it, and where it fits

**In one sentence:** Vespa fits when a client needs relevance *and* scale *and* real-time together — RAG at scale, recommendation/personalization, and hybrid e-commerce/marketplace search.

Verified adopters (named on vespa.ai's own case studies — present the vendor-reported numbers *as* vendor-reported):
- **Yahoo** — the origin; ~150 apps, ~800K qps, ~1B users.
- **Perplexity** — built its RAG retrieval layer on Vespa (chunk-level retrieval, hybrid ranking, real-time indexing, in-serving inference). Roughly tens of millions of users / ~100M+ queries per week (Vespa's two pages quote different figures — say "approximately").
- **Spotify** — semantic/podcast search via dense retrieval.
- **Vinted** — replaced Elasticsearch; Vespa-reported outcomes include ~50% infra reduction and 2.5× lower latency.
- Others named: **Elicit, Qwant, Kleinanzeigen, Onyx/Danswer, RavenPack/Bigdata.com**.

> ⚠️ *Some third-party blog posts list Indeed / AlphaSense / Taboola / Farfetch as users — these were **not** confirmed on a primary source during research. Don't put them on a slide as fact.*

**For an AI/ML services company like Factored**, the pitch is: *"When a client outgrows 'Elasticsearch + a vector DB + a reranker,' Vespa is the consolidation play — and the value-add is the schema design, ranking-pipeline, and relevance-tuning expertise it demands."*

---

## 12. Honest trade-offs (so you sound credible, not like a salesperson)

- **Steeper learning curve.** YQL, schemas, ranking expressions, and the tensor model are unfamiliar to teams without search-engineering background. Plan for weeks, not days, to production.
- **Heavier to operate than a hosted vector DB for small jobs.** For a quick prototype or a few million vectors, a managed vector DB (Pinecone) stands up faster. Vespa Cloud removes the infra burden but it's still a more capable — and more complex — system.
- **Custom logic is JVM/Java.** Custom searchers and document processors are Java OSGi components built with Maven (no lightweight inline scripting like Elasticsearch "painless"). The built-in features cover a lot, but deep customization means writing Java.
- **Smaller community / talent pool** than Elasticsearch — fewer engineers have hands-on Vespa experience. (A staffing risk for clients, an *opportunity* for a services firm.)

### When to choose what

| Need | Best fit |
|------|----------|
| Large-scale hybrid retrieval + custom ML ranking + real-time updates in one system | **Vespa** |
| Simple, managed, vector-first semantic search; small/medium scale | Pinecone / Qdrant / Weaviate |
| General-purpose text search, huge ecosystem, easy ops | Elasticsearch / OpenSearch |
| Billion-scale vectors, vector-DB-centric | Milvus / Vespa |

---

## 13. Deployment modes

- **Local Docker** (what the capstone uses): one `vespaengine/vespa` container runs everything. Ports **8080** (query/feed) and **19071** (config/deploy). Needs ~4 GB RAM.
- **Self-hosted multi-node**: you declare the topology (clusters, nodes, redundancy) in `services.xml` + `hosts.xml`, and deploy through the config server. You operate it.
- **Vespa Cloud** (managed): they run it; you `vespa deploy` to dev/prod zones and they handle upgrades and autoscaling. **Enclave** runs the managed system *inside your own AWS/GCP account* so data never leaves your cloud.

**Release cadence:** Vespa ships several releases per week (Mon–Thu). Current line as of mid-2026 is **8.7xx** (Vespa 8). Don't be surprised by the fast-moving version numbers — that's normal.

---

## 14. Glossary (the words to say correctly)

| Term | Meaning |
|------|---------|
| **Application package** | The deployable directory (services.xml + schemas + components). The only way to change Vespa. |
| **services.xml** | Declares clusters, nodes, components. |
| **Schema (.sd)** | Defines a document type, its fields, indexing, and rank profiles. |
| **Container cluster (jdisc)** | Stateless JVM layer: handles requests, runs searchers/embedders, global-phase ranking. |
| **Content cluster** | Stateful layer: stores docs, matches, runs first/second-phase ranking. |
| **proton** | The C++ search core on each content node. |
| **Indexing modes** | `index` (inverted index/BM25/HNSW), `attribute` (in-memory column, for rank/filter/sort), `summary` (returnable). |
| **Tensor** | Vespa's multi-dimensional type: indexed (dense) / mapped (sparse) / mixed. Embeddings live here. |
| **Embedder** | Built-in component that turns text → tensor at index and query time (e.g. hugging-face-embedder). |
| **HNSW** | The graph index enabling approximate nearest-neighbor (ANN) vector search. |
| **nearestNeighbor / closeness** | YQL operator for ANN retrieval / the 0–1 similarity rank feature. |
| **Rank profile** | A named scoring recipe: first-phase / second-phase / global-phase expressions over rank features. |
| **BM25** | The standard lexical relevance score (`bm25(field)`). |
| **Hybrid search** | Lexical + vector retrieval in one query, fused in ranking (e.g. RRF). |
| **YQL** | Vespa's SQL-like query language. |
| **RRF** | Reciprocal Rank Fusion — combine rankings by position, not raw score. |
| **RAGSearcher** | Built-in component that does retrieval → prompt → LLM → streamed answer. |
| **Partial update** | In-memory write to an attribute; searchable instantly. |

---

### Where to go deeper (official, current)
- Quick start (Docker): https://docs.vespa.ai/en/vespa-quick-start.html
- pyvespa getting started: https://vespa-engine.github.io/pyvespa/getting-started-pyvespa.html
- Hybrid search tutorial (the one the capstone follows): https://docs.vespa.ai/en/tutorials/hybrid-search.html
- Ranking & phased ranking: https://docs.vespa.ai/en/ranking.html · https://docs.vespa.ai/en/phased-ranking.html
- Tensors: https://docs.vespa.ai/en/tensor-user-guide.html
- Embeddings: https://docs.vespa.ai/en/embedding.html
- RAG: https://docs.vespa.ai/en/rag/rag.html · blog: https://blog.vespa.ai/the-rag-blueprint/
- Sample apps: https://github.com/vespa-engine/sample-apps
- Free course: https://pyvespa.readthedocs.io / https://blog.vespa.ai and the "learn.vespa.ai" labs

Now go build it → [../capstone/README.md](../capstone/README.md).
