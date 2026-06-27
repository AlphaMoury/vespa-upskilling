# Vespa Cheatsheet (keep this open while you build)

## Mental model in 6 lines
- **Application package** = the deployable directory (`services.xml` + `schemas/` + `components/`). The only way to change Vespa.
- **Container cluster** (stateless, JVM) = handles requests, runs embedders + searchers + global-phase ranking.
- **Content cluster** (stateful) = stores docs, matches, runs first/second-phase ranking. "Compute close to the data."
- **Schema (.sd)** = document type + fields + indexing + rank profiles.
- **Rank profile** = your scoring math, in phases (first → second → global).
- **YQL** = the query language. `ranking=<profile>` picks the scoring recipe.

## The three indexing modes (memorize this)
| `indexing:` token | builds | use it for |
|---|---|---|
| `index` | inverted index | full-text match, `bm25()`, HNSW on tensors |
| `attribute` | in-memory column | filter, sort, group, **rank access**, ANN/`closeness` |
| `summary` | returnable | showing the field in results |

Combine with `|`: `indexing: summary | index | attribute`.

---

## Schema (.sd) — native form
```
schema doc {
    document doc {
        field id    type string { indexing: summary }
        field title type string { indexing: summary | index   index: enable-bm25 }
        field body  type string { indexing: summary | index   index: enable-bm25 }
        field price type float  { indexing: summary | attribute }
        field embedding type tensor<float>(x[384]) {
            indexing: input title . " " . input body | embed e5 | attribute | index
            attribute { distance-metric: angular }
            index { hnsw { max-links-per-node: 16  neighbors-to-explore-at-insert: 200 } }
        }
    }
    fieldset default { fields: title, body }

    rank-profile bm25     { first-phase { expression: bm25(title) + bm25(body) } }
    rank-profile semantic {
        inputs { query(q) tensor<float>(x[384]) }
        first-phase { expression: closeness(field, embedding) }
    }
    rank-profile fusion inherits bm25 {
        inputs { query(q) tensor<float>(x[384]) }
        first-phase { expression: closeness(field, embedding) }
        global-phase {
            expression: reciprocal_rank_fusion(bm25(title)+bm25(body), closeness(field, embedding))
            rerank-count: 1000
        }
    }
}
```

## Rank features you'll actually use
- `bm25(field)` — lexical score (needs `index: enable-bm25`).
- `closeness(field, embedding)` — 0–1 vector similarity. `distance(field, embedding)` — raw.
- `attribute(name)`, `freshness(ts)`, `if(cond, a, b)`, `log()`, `+ - * /`.
- `xgboost("m.json")`, `lightgbm(...)`, `onnx(model)` — ML models from `models/`.
- `match-features { ... }` — return feature values per hit to debug *why* it ranked.

## Distance metrics (match your model!)
`angular` (cosine, normalized embeddings like e5) · `euclidean` (default) · `dotproduct` · `prenormalized-angular` · `hamming` (binary/int8).

---

## YQL quick reference
```sql
-- keyword
select * from doc where userQuery() limit 10                         ranking=bm25

-- semantic (ANN)
select * from doc where {targetHits:100}nearestNeighbor(embedding,q) limit 10   ranking=semantic
-- + body: input.query(q) = embed(e5, "the query text")

-- hybrid (match either, fuse in ranking)
select * from doc where userQuery() or ({targetHits:100}nearestNeighbor(embedding,q)) limit 10   ranking=fusion

-- hybrid via rank() : first arg matches, rest only feed rank features
select * from doc where rank(({targetHits:100}nearestNeighbor(embedding,q)), userQuery()) limit 10

-- filters
... where userQuery() and range(price, 10, 100) and category contains "shoes"

-- grouping / facets
select * from doc where true |
  all( group(category) max(10) order(-count()) each( output(count(), avg(price)) ) )
```
Operators: `contains`, `phrase(...)`, `range(f,lo,hi)`, `and / or / !`, `order by relevance desc`, `limit`, `offset`, `timeout`.

---

## pyvespa essentials (Python)
```python
from vespa.package import (ApplicationPackage, Schema, Document, Field, FieldSet,
                           RankProfile, Function, GlobalPhaseRanking, HNSW, Component, Parameter)
from vespa.deployment import VespaDocker

# define → deploy → feed → query
app = VespaDocker().deploy(application_package=package)     # returns a live Vespa app on :8080

app.feed_iterable(docs, schema="doc", namespace="tutorial", callback=cb)   # docs: {"id":..,"fields":{..}}
#   NOTE: do NOT include the embedding field — Vespa generates it via the `embed` step.

with app.syncio(connections=1) as s:
    r = s.query(
        yql="select * from sources * where userQuery() or ({targetHits:100}nearestNeighbor(embedding,q)) limit 5",
        query="how do fruits and vegetables help asthma",       # fills userQuery() / BM25
        ranking="fusion",
        body={"input.query(q)": "embed(e5, \"how do fruits and vegetables help asthma\")"},
    )
for hit in r.hits:
    print(hit["relevance"], hit["fields"]["title"])
```
- Connect to an already-running Vespa without redeploying: `from vespa.application import Vespa; app = Vespa(url="http://localhost", port=8080)`.
- Single embedder → `embed("text")` works; multiple → must say `embed(e5, "text")`.
- Updates: `app.update_data(schema, data_id, fields)` · deletes: `app.delete_data(schema, data_id)`.

## Gotchas (the ones that bite)
- **Python must be 3.10–3.13** for pyvespa (you have 3.14 → use the `uv` venv from `setup.sh`).
- **Tensor dim must match the model** — e5-small-v2 is **384**. Mismatch = silent bad results.
- **`distance-metric` must match the model** — e5 → `angular`.
- **`closeness` needs `attribute`** in the field's indexing; HNSW gives *approximate* NN, no HNSW = exact brute force (slower, still correct).
- Old pyvespa `feed_batch` / `query_batch` are **deprecated** — use `feed_iterable` + callback.
- Embedding field must be `is_document_field=False` when it's generated by `embed` (don't feed it).

---

## CLI (if you use the `vespa` binary instead of Python)
```bash
vespa config set target local
vespa deploy --wait 300 ./app          # deploy an application package dir
vespa feed docs.jsonl                   # feed JSONL
vespa query 'yql=select * from doc where true' ranking=bm25
vespa status                            # health
```

## Docker one-liner (what pyvespa does for you)
```bash
docker run --detach --name vespa --hostname vespa-container \
  -p 8080:8080 -p 19071:19071 vespaengine/vespa
```
8080 = query/feed · 19071 = config/deploy.
