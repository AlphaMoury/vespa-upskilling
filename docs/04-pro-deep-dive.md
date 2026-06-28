# Going Pro: Deep Technical Fluency in Vespa

> This is the "become dangerous" document. The [deep dive](01-deep-dive.md) gives you the mental model; this gives you the **engineer's command of the toolset** — enough to design schemas, tune ranking, debug relevance, extend the engine, and operate it. Python/pyvespa stays your day-to-day driver; the goal here is that you *understand what pyvespa generates* and can drop to the native form (`.sd`, `services.xml`, ranking expressions, Java, the CLI) whenever you need to.
>
> All syntax verified against docs.vespa.ai (Vespa 8.x, 2026-06). A few edge-case spellings change between releases — those are flagged `↪ check the reference`. When in doubt, the reference docs linked at the end are ground truth.

## The pro mindset (read this first)

Three habits separate someone fluent in Vespa from someone following a tutorial:

1. **You think in the application package, not the API.** pyvespa, the CLI, and the REST API are all just ways to produce/deploy one thing: an **application package** (`services.xml` + `schemas/*.sd` + components/models). The engine only ever sees that. So: *write Python, but read the generated `.sd`*. (`package.to_files("generated_app")` — do it often.)
2. **You think in phases and cost.** Every relevance/perf decision is "which phase, how many docs, how expensive." Retrieval → first-phase → second-phase → global-phase is the spine of the whole system.
3. **You measure.** Pros don't argue about whether hybrid is better — they collect `match-features`, label data, and compute nDCG. Relevance is an experimental science here.

---

## Table of contents
1. [Application package internals (read the native form)](#1-application-package-internals)
2. [Schema & the indexing language, in depth](#2-schema--the-indexing-language-in-depth)
3. [Vector search & embeddings, tuned](#3-vector-search--embeddings-tuned)
4. [Ranking mastery](#4-ranking-mastery)
5. [ML in ranking: ONNX, GBDT, and the LTR workflow](#5-ml-in-ranking-onnx-gbdt-and-the-ltr-workflow)
6. [The query stack: YQL, weakAnd/WAND, grouping, summaries](#6-the-query-stack)
7. [Performance & sizing](#7-performance--sizing)
8. [Extending Vespa with Java components](#8-extending-vespa-with-java-components)
9. [Operating Vespa: CLI, metrics, deployment](#9-operating-vespa)
10. [Relevance engineering & evaluation](#10-relevance-engineering--evaluation)
11. [The fluency checklist](#11-the-fluency-checklist)

---

## 1. Application package internals

The package is a directory; the only strictly required file is `services.xml`. Full layout:

```
my-app/
├── services.xml                 # REQUIRED — topology: clusters, nodes, components
├── hosts.xml                    # self-hosted only — maps hostalias → real hostname
├── deployment.xml               # Vespa Cloud — environments/regions/CD pipeline
├── validation-overrides.xml     # allow an otherwise-blocked (destructive) deploy
├── schemas/
│   └── doc.sd                   # one .sd per document type (filename == schema name)
├── search/query-profiles/       # named bundles of default query parameters
├── components/                  # custom Java OSGi bundles (.jar)
├── models/                      # ONNX / XGBoost / LightGBM model files
├── constants/                   # large constant tensors for ranking
└── security/clients.pem         # Vespa Cloud data-plane mTLS trust anchor
```

### services.xml — the topology

`<services version="1.0">` holds `<container>` (stateless request layer) and `<content>` (stateful storage/search), plus optional `<admin>` and `<routing>`. A single-node local app with an embedder:

```xml
<?xml version="1.0" encoding="utf-8"?>
<services version="1.0">

  <container id="default" version="1.0">
    <document-api/>            <!-- enables feed/get/visit endpoints -->
    <search/>                  <!-- enables the query stack -->
    <document-processing/>     <!-- runs docproc/indexing chains -->

    <component id="e5" type="hugging-face-embedder">
      <transformer-model url="https://data.vespa-cloud.com/sample-apps-data/e5-small-v2-int8/e5-small-v2-int8.onnx"/>
      <tokenizer-model   url="https://data.vespa-cloud.com/sample-apps-data/e5-small-v2-int8/tokenizer.json"/>
    </component>

    <nodes><node hostalias="node1"/></nodes>
  </container>

  <content id="content" version="1.0">
    <min-redundancy>1</min-redundancy>                <!-- copies per document -->
    <documents>
      <document type="doc" mode="index"/>             <!-- index | streaming -->
    </documents>
    <nodes>
      <node distribution-key="0" hostalias="node1"/>  <!-- distribution-key: unique int per content node -->
    </nodes>
  </content>

</services>
```

Things a pro notices:
- **`<component type="hugging-face-embedder">`** is the *native* form of what pyvespa's `Component(id="e5", type="hugging-face-embedder", ...)` generates. `<transformer-model>`/`<tokenizer-model>` accept `path=` (file in package), `url=`, or `model-id=` (Cloud model hub). Useful extras: `<pooling-strategy>mean|cls</pooling-strategy>`, `<normalize>true</normalize>`, and `<prepend><query>query:</query><document>passage:</document></prepend>` (important for E5 models — they're trained with those prefixes).
- **`mode="index"`** = full inverted+vector indexes. **`mode="streaming"`** = no index, stream-scan a selected slice — the right call for per-user data (mailboxes, per-tenant docs) where you always restrict by a group key.
- **`min-redundancy`** is the modern replacement for `<redundancy>`; with multiple groups, one copy per group is kept. `searchable-copies` (under `<engine><proton>`) controls how many replicas are kept *indexed/ready* in memory.

Self-hosted adds `hosts.xml` (`<host name="localhost"><alias>node1</alias></host>`) and usually an `<admin version="2.0">` block declaring configservers, slobroks, cluster-controllers, logserver. On Vespa Cloud you omit `<admin>`/`hosts.xml` and use `<nodes count="2"><resources vcpu="4" memory="16Gb" disk="100Gb"/></nodes>`.

---

## 2. Schema & the indexing language, in depth

Grammar: `schema NAME { document NAME { field ... } fieldset ... document-summary ... rank-profile ... }`.

### The three destinations (the decision you make per field)

- **`index`** → inverted text index (strings) or HNSW graph (tensors, *also needs `attribute`*). Enables matching + BM25.
- **`attribute`** → in-memory column. Enables filtering, sorting, grouping, **ranking access**, and nearest-neighbor.
- **`summary`** → returnable in results.

### The indexing language

A pipe pipeline: `indexing: input <field> | <op> | ... | <destination>`. `.` concatenates. Key ops: `input`, `embed <id>`, `tokenize`, `lowercase`, `trim`, `normalize`, `to_array`, `split`/`join`, `for_each { ... }`, `set_language`, `if`, `||` (fallback).

Five fields that show the range — this is the "read the native form" muscle:

```
schema doc {
  document doc {

    # 1) text → inverted index + summary, BM25, dynamic snippet, stemming
    field body type string {
      indexing: summary | index
      index: enable-bm25
      match: text
      stemming: best
      bolding: on
    }

    # 2) numeric attribute as a fast filter (no ranking term)
    field price type float {
      indexing: summary | attribute
      attribute: fast-search       # B-tree dict → fast selective lookups
      rank: filter                 # pure filter, skip match scoring
    }

    # 3) embedding generated in-engine, HNSW ANN  (== pyvespa's embedding field)
    field embedding type tensor<float>(x[384]) {
      indexing: input title . " " . input body | embed e5 | attribute | index
      attribute { distance-metric: angular }     # distance-metric goes INSIDE attribute{}
      index { hnsw { max-links-per-node: 16  neighbors-to-explore-at-insert: 100 } }
    }

    # 4) tags → weightedset attribute (term → weight), fast-search
    field tags type weightedset<string> {
      indexing: attribute | summary
      attribute: fast-search
    }

    # 5) array-of-struct with selectively-indexed sub-fields
    struct review { field rating type int {}  field text type string {} }
    field reviews type array<review> {
      indexing: summary
      struct-field rating { indexing: attribute  attribute: fast-search }
      struct-field text   { indexing: index  match: text }
    }
  }

  fieldset default { fields: title, body, tags }
}
```

Pro notes:
- **`distance-metric` belongs inside `attribute { }`** (a very common mistake is putting it at field level). Valid values: `euclidean`, `angular`, `prenormalized-angular`, `dotproduct`, `hamming`, `geodegrees`.
- **`match: exact` / `match: word` / `match: gram` (+ `gram-size`)** change tokenization — exact for IDs/SKUs, gram for substring/CJK.
- **`document-summary`** lets you define lighter result views: `document-summary short { summary title { source: title } }` and request it with `presentation.summary=short`. Add `dynamic` for query-relevant snippets.
- **Parent/child:** `field campaign_ref type reference<campaign> { indexing: attribute }` + `import field campaign_ref.budget as budget {}`; the parent type must be `global="true"` in services.xml. Lets you rank on parent fields without denormalizing.

---

## 3. Vector search & embeddings, tuned

### HNSW: the knobs that matter

```
index { hnsw {
  max-links-per-node: 16                 # graph degree. ↑ = better recall, more memory
  neighbors-to-explore-at-insert: 100    # build effort. ↑ = better graph, slower feed
} }
```
Query-time recall/cost is controlled by **`targetHits`** (per content node) in the `nearestNeighbor` operator, optionally widened with **`hnsw.exploreAdditionalHits`**. ANN is *approximate* by default; force exact brute force with `{approximate:false}` (fine for a few thousand docs; the capstone uses exact-or-HNSW interchangeably at this scale).

### Distance metric must match the model

E5/normalized embeddings → `angular` (or `prenormalized-angular` if you pre-normalize). Binary/int8 vectors → `hamming`. Get this wrong and recall silently collapses — a classic debugging trap.

### Memory levers for big vector sets

- **Cell type** sets precision/size: `tensor<float>` (4B), `tensor<bfloat16>` (2B), `tensor<int8>` (1B). Halving precision ~halves memory with minor quality loss — standard at scale.
- **Binarization:** store `int8` binary vectors with `hamming` distance for cheap first-pass retrieval, then re-rank survivors with full-precision (the RAG Blueprint does exactly this: 96-dim binarized int8 retrieval → richer ranking).
- **Multi-vector per document:** a mixed tensor `tensor<float>(p{},x[384])` stores one vector per passage/chunk; `nearestNeighbor` matches the *best* chunk. This is how you do long documents and ColBERT-style late interaction.

### Embedders (native + pyvespa)

The same `embed` op runs document-side (indexing) and query-side. Beyond `hugging-face-embedder`: `colbert-embedder` (late interaction, one vector/token), `splade-embedder` (learned sparse lexical), and API embedders (OpenAI/Cohere/Voyage/Mistral). For E5 specifically, configure `<prepend>` query/passage prefixes for best quality.

---

## 4. Ranking mastery

This is where Vespa pays off. A rank-profile is a scoring program with phases.

```
rank-profile pro inherits default {

  inputs {
    query(q) tensor<float>(x[384])        # query embedding (set via input.query(q))
    query(alpha) double                   # a runtime scalar knob
  }

  constants { boost tensor<float>(x[3]): [0.1, 0.2, 0.7] }

  function bm25sum()    { expression: bm25(title) + bm25(body) }
  function vec()        { expression: closeness(field, embedding) }
  function textScore(f) { expression: 0.7*bm25(f) + 0.3*nativeProximity(f) }  # functions take args

  first-phase  { expression: bm25sum + 5*vec() }                  # cheap, EVERY matched doc
  second-phase { expression: xgboost("ltr.json")  rerank-count: 1000 }  # per-node top-K
  global-phase {                                                   # container, merged hits
    expression: reciprocal_rank_fusion(bm25sum, vec())
    rerank-count: 100
  }

  match-features   { bm25(title) bm25(body) vec() attribute(price) }  # exported per hit (LTR + reuse)
  summary-features { closeness(field, embedding) }                    # debug view
}
```

### Phases — where to put what

| Phase | Runs on | Scores | Put here |
|-------|---------|--------|----------|
| retrieval (YQL ops) | content | all candidates | ANN, weakAnd, wand, filters |
| **first-phase** | content | **every match** | cheap linear combo: `bm25`, `closeness`, `freshness` |
| **second-phase** | content | top `rerank-count`/node | GBDT, `fieldMatch`, heavier features |
| **global-phase** | container | top `rerank-count` merged | ONNX cross-encoder; **cross-hit** normalize/RRF |

Golden rules: keep `first-phase` O(cheap) (it scales with the matched set); bound `second-phase` with `rerank-count`; **normalizers and fusion are only correct in `global-phase`** (they need all merged candidates).

### Rank features worth memorizing

- Text: `bm25(f)`, `nativeRank`, `nativeProximity(f)`, `fieldMatch(f)` (+ `.proximity`, `.completeness`, `.significance`… — accurate but expensive, keep out of first-phase).
- Vector: `closeness(field, emb)` (≈1 when close; cheap because the ANN operator computed it), `distance(field, emb)`.
- Structured/recency: `attribute(name)`, `freshness(ts)` (0–1), `age(ts)`, `now`.
- Query terms: `term(i).significance` (IDF), `term(i).weight`, `queryTermCount`.

### Tensor expressions (compute in the ranker)

```
sum(query(q) * attribute(embedding))                 # dot product (cosine if normalized)
sum(query(q_cat) * attribute(cat_scores))            # sparse weighted-set dot product
```
Primitives: `reduce`, `join`, `map`, `matmul`, `cosine_similarity`, `l2_normalize`, `softmax`. This is the same machinery that runs models — "search" and "inference" really are one operation.

### Normalizers / fusion (global-phase only)

- `normalize_linear(f)` → min-max across the hit set.
- `reciprocal_rank(f, k=60)` → `1/(k+rank)`.
- `reciprocal_rank_fusion(a, b, …)` → sum of RRF over inputs — the robust hybrid combiner (scale-free). This is exactly your capstone's `fusion` profile.

### Control knobs

`rank-score-drop-limit` (prune low scorers between phases), `num-threads-per-search` (parallelize one query), `match-phase { attribute order max-hits }` (graceful degradation when hit counts explode), and the query-side `ranking.softtimeout.enable` (return best-so-far instead of timing out). `↪ check the reference` for exact second-phase global-cap spelling, which has varied across releases.

---

## 5. ML in ranking: ONNX, GBDT, and the LTR workflow

Models live in `models/` and deploy with the package.

### ONNX (transformers, cross-encoders)

```
rank-profile cross_encoder inherits default {
  onnx-model reranker {
    file: models/reranker.onnx
    input  "input_ids":     query(q_tokens)
    input  "doc_embedding": attribute(embedding)
    output "logits":        score
  }
  first-phase  { expression: closeness(field, embedding) }   # cheap recall
  global-phase { expression: sum(onnx(reranker).score)  rerank-count: 40 }  # heavy CE, container
}
```
Put ONNX in `second-phase` (per-node) or `global-phase` (container — best for big transformers). **Never first-phase.**

### GBDT (XGBoost / LightGBM) — the workhorse re-ranker

```
rank-profile gbdt inherits base_features {
  first-phase  { expression: nativeRank }
  second-phase { expression: xgboost("ltr.json")  rerank-count: 1000 }
}
```
Vespa maps the model's feature names to rank features (or to `function`s of the same name you define). LightGBM: `lightgbm("model.json")`.

### The learning-to-rank workflow (the pro relevance loop)

1. **Export features.** Make a collection profile whose `match-features { … }` lists every candidate signal, with a neutral `first-phase` (e.g. `random`) so you don't bias the sample.
2. **Collect.** Run judged queries with `ranking.profile=collect-training-data`; pull per-hit `match-features` into a table. Each row = features + a relevance **label** (from qrels or click logs).
3. **Train offline.** Logistic regression for first-phase *weights*; a GBDT on the full feature set for second-phase.
4. **Deploy.** Drop the model in `models/`, reference it with `xgboost(...)`, bake the linear weights into `first-phase`, redeploy.
5. **Measure.** Re-run nDCG. Iterate.

This is the loop behind the **RAG Blueprint** ("the architecture that powers Perplexity"): cheap learned-linear first-phase over lexical+vector features → GBDT second-phase. (Your capstone's `03_evaluate.py` is step 5 of this loop in miniature; [Lab 1](05-advanced-labs.md) wires up steps 1–2.)

---

## 6. The query stack

### YQL beyond the basics

```sql
-- weakAnd: OR-recall but only ~targetHits/node fully scored (the scalable "OR")
select * from doc where {targetHits:200}weakAnd(default contains "fruit", default contains "asthma")

-- wand: max-inner-product over ONE weightedset field with explicit weights
select * from doc where {targetHits:25}wand(tags, {"diet":1, "cancer":2})

-- rank(): first arg drives matching/recall; later args ONLY add features for scoring
select * from doc where rank(({targetHits:100}nearestNeighbor(embedding,q)), userQuery())

-- userInput / filters / structured
select * from doc where userInput(@q) and range(price,10,100) and tags contains "vegan"
```
- **`weakAnd`** uses term significance (IDF), not document term frequency, for its internal threshold; `targetHits` is the recall/cost dial. Config `weakAnd.replace:true` auto-rewrites plain OR into weakAnd.
- **`rank(A, B, …)`**: A = the recall set; B… don't restrict matching but their features (e.g. `bm25`, `rawScore`) become available to the ranker. The canonical "retrieve cheap, score rich" pattern.

### Query profiles

`search/query-profiles/default.xml` holds default request params (ranking profile, hits, timeout, presentation) so clients send less and behavior is centralized. Override per request.

### Grouping & aggregation (facets, like SQL GROUP BY)

```sql
select * from doc where userQuery() |
  all( group(category) max(10) order(-count())
       each( output(count(), avg(price)) ) )
```
Nest `group`/`each`, aggregate with `count/sum/avg/min/max`. Powers faceted UIs, analytics, and diversity.

### Streaming search

For per-user/grouped corpora: `mode="streaming"`, document ids carry a group (`id:ns:doc:g=user123:42`), queries pass `streaming.groupname=user123`. No index to maintain, cheap storage, exact matching over a small slice — ideal for "search within *my* documents."

---

## 7. Performance & sizing

The mental model: **attributes cost RAM, indexes cost disk + give matching, ranking cost = phase placement × docs scored.**

- **`attribute: fast-search`** adds a dictionary → fast filtering on selective values, at memory cost. Add it when a numeric/string attribute is a frequent *filter*; skip it for fields only read in ranking.
- **`paged` attributes** spill to disk-backed memory (mmap) → big memory savings for rarely-touched attributes.
- **HNSW** memory ≈ vectors × cell-size + graph links; tune `max-links-per-node` and cell type. Binarize for the first pass.
- **Phased ranking is your main latency lever:** push cost from first-phase (every doc) to second/global-phase (bounded by `rerank-count`). A heavy `fieldMatch` or ONNX in first-phase will melt at scale.
- **`num-threads-per-search`** trades CPU for latency on a single expensive query.
- **`match-phase` degradation + `softtimeout`** keep tail latency bounded under load (return best-so-far, sorted by a quality attribute).
- **Feed throughput:** attributes update in-place (no reindex) → partial updates are cheap and instantly searchable; use `vespa-feed-client` (HTTP/2, async) for bulk.

Capacity-plan from: corpus size × per-doc memory (attributes + vector cells + index), query rate × per-query cost (matched-set size × first-phase cost + rerank-count × second-phase cost). Vespa exposes all of it via metrics (§9).

---

## 8. Extending Vespa with Java components

You reach for Java when built-ins aren't enough: custom query rewriting, blending, business logic, external calls, custom endpoints, bespoke document enrichment. Components are **OSGi bundles** built with Maven; packaging is `container-plugin`; **JDK ≥ 17**.

### Searcher — intercept/transform queries and results

```java
package ai.factored.vespa;

import com.yahoo.search.*;
import com.yahoo.search.searchchain.Execution;
import com.yahoo.search.result.Hit;

public class DefaultRankingSearcher extends Searcher {
    @Override
    public Result search(Query query, Execution execution) {
        // business rule: if caller didn't choose a profile, default to hybrid
        if (query.getRanking().getProfile().equals("default"))
            query.getRanking().setProfile("fusion");

        Result result = execution.search(query);     // run the rest of the chain
        result.hits().add(new Hit("meta", 0.0) {{ setField("profile", query.getRanking().getProfile()); }});
        return result;
    }
}
```
Register and order it in a search chain:
```xml
<search>
  <chain id="default" inherits="vespa">
    <searcher id="ai.factored.vespa.DefaultRankingSearcher" bundle="pro-java-searcher"/>
  </chain>
</search>
```
`@Before`/`@After`/`@Provides` annotations order searchers relative to phases/each other; `bundle=` is the Maven `artifactId`.

### DocumentProcessor — transform documents on the write path

```java
public class TitleNormalizer extends DocumentProcessor {
    @Override public Progress process(Processing processing) {
        for (var op : processing.getDocumentOperations())
            if (op instanceof DocumentPut put) {
                var doc = put.getDocument();
                var t = (StringFieldValue) doc.getFieldValue("title");
                if (t != null) doc.setFieldValue("title", new StringFieldValue(t.getString().trim()));
            }
        return Progress.DONE;
    }
}
```
**Must be thread-safe** — one instance serves many threads; no mutable instance state.

### Also: `RequestHandler` (custom HTTP endpoints bound to a URI), `Renderer` (custom result formats), and **custom config** via `.def` files injected as a generated `ConfigInstance`. See [pro-java-searcher/](../pro-java-searcher/) for a buildable example.

---

## 9. Operating Vespa

### The `vespa` CLI (your native control plane)

```bash
vespa config set target local                       # or: cloud
vespa config set application my-tenant.my-app.default
vespa deploy --wait 300 .                            # build (if Maven) + deploy + activate
vespa status                                         # is the container serving?
vespa feed docs.jsonl                                # bulk feed JSONL
vespa query 'yql=select * from doc where userQuery()' 'query=asthma' ranking=fusion hits=10
vespa document get id:tutorial:doc::MED-1
vespa visit                                          # stream all docs out
```
Feed JSONL is one op/line: `{"put":"id:tutorial:doc::1","fields":{"title":"…","body":"…"}}` (also `update`/`remove`).

### Health, metrics, logs (know these endpoints cold)

| Endpoint (`:8080` container) | Use |
|---|---|
| `/state/v1/health` | up/down |
| `/state/v1/metrics` | per-node metrics snapshot |
| `/metrics/v2/values` | aggregated, all nodes |
| `/prometheus/v1/values?consumer=vespa` | Prometheus scrape |
| `/ApplicationStatus` | app/component status |

Config server is `:19071` (deploy API). `vespa-logfmt -l warning,error` for readable logs (self-hosted).

### Vespa Cloud (managed, native)

```bash
vespa auth login            # control-plane (browser device flow)
vespa auth cert             # data-plane mTLS cert/key → writes security/clients.pem
vespa deploy -z dev.aws-us-east-1c .     # dev zone, immediate
vespa prod deploy .                       # submit to CD pipeline (test→staging→prod)
```
`deployment.xml` drives the pipeline — `<instance>`, `<prod><region>`, `<delay>` (canary bake time), `<block-change>` (freeze windows), and `<test>`/`<staging>` gates that run your JUnit **system-test**/**staging-test** before any prod region. This is how you ship Vespa changes safely with zero-downtime rolling upgrades.

---

## 10. Relevance engineering & evaluation

The skill that makes you valuable. The loop:

1. **A golden set.** Queries + judged relevant docs (qrels), or click logs as weak labels. NFCorpus (your capstone) ships qrels — that's why it's a great practice corpus.
2. **A metric.** nDCG@k (graded, position-discounted — the default), plus recall@k, MRR. `03_evaluate.py` computes nDCG@10.
3. **Experiments = rank profiles.** Each hypothesis ("add freshness", "weight title 2×", "GBDT instead of linear") is a new `rank-profile`. Run the golden set under each; compare. Because profiles are just config, A/B is cheap.
4. **Feature collection** via `match-features` feeds offline training (the LTR loop in §5).
5. **Online**: trace with `trace.level` / `ranking.listFeatures` to see *why* a doc scored; watch metrics for latency regressions.

Pros instrument relevance like a test suite: a fixed query set, a tracked nDCG number, no merge that drops it without a reason.

---

## 11. The fluency checklist

You're fluent when you can, without notes:

- [ ] Read a `.sd` and predict what's matchable, rankable, returnable, and what it costs in RAM vs disk.
- [ ] Explain `index` vs `attribute` vs `summary` and when each is required.
- [ ] Write a hybrid rank-profile with first-phase + global-phase RRF, and say why fusion must be global-phase.
- [ ] Place a feature in the right phase given its cost and the matched-set size.
- [ ] Pass a query tensor (`input.query(q)`), write a `nearestNeighbor` + `rank()` YQL, and read the hit's features.
- [ ] Tune HNSW (`max-links-per-node`, `targetHits`) and pick the right `distance-metric` for a model.
- [ ] Run the LTR loop: export `match-features`, train a GBDT, deploy it to second-phase.
- [ ] Stand up the app natively (`vespa deploy`), feed JSONL, query, and read `/metrics/v2/values`.
- [ ] Write a Searcher or DocumentProcessor and wire it into a chain.
- [ ] Measure a relevance change with nDCG instead of guessing.

Work the [Advanced Labs](05-advanced-labs.md) until every box is checked — that's the difference between "I read about Vespa" and "I'm fluent in Vespa."

---

### Reference (ground truth)
- Schema: https://docs.vespa.ai/en/reference/schema-reference.html · Indexing language: https://docs.vespa.ai/en/reference/indexing-language-reference.html
- services.xml: https://docs.vespa.ai/en/reference/services.html (container/content/admin sub-pages)
- Ranking: https://docs.vespa.ai/en/ranking.html · Phased: https://docs.vespa.ai/en/phased-ranking.html · Expressions: https://docs.vespa.ai/en/reference/ranking-expressions.html · Features: https://docs.vespa.ai/en/reference/rank-features.html
- Tensors: https://docs.vespa.ai/en/tensor-user-guide.html · HNSW: https://docs.vespa.ai/en/approximate-nn-hnsw.html · Embedding: https://docs.vespa.ai/en/embedding.html
- ONNX: https://docs.vespa.ai/en/onnx.html · XGBoost: https://docs.vespa.ai/en/xgboost.html · LightGBM: https://docs.vespa.ai/en/lightgbm.html · LTR: https://docs.vespa.ai/en/learning-to-rank.html
- Query API: https://docs.vespa.ai/en/query-api.html · YQL: https://docs.vespa.ai/en/query-language.html · WAND: https://docs.vespa.ai/en/using-wand-with-vespa.html · Grouping: https://docs.vespa.ai/en/grouping.html
- Searchers: https://docs.vespa.ai/en/searcher-development.html · Doc processing: https://docs.vespa.ai/en/document-processing.html · Bundles: https://docs.vespa.ai/en/components/bundles.html
- CLI: https://docs.vespa.ai/en/vespa-cli.html · Metrics: https://docs.vespa.ai/en/reference/metrics.html · Cloud: https://cloud.vespa.ai/ · Automated deployments: https://docs.vespa.ai/en/operations/automated-deployments.html
