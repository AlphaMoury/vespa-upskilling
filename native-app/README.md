# Native application package — deploy Vespa the way its engineers do

This is the **same hybrid-search app as the Python capstone**, expressed as a raw Vespa
application package (`services.xml` + `schemas/doc.sd`) and driven entirely by the `vespa`
CLI. No pyvespa. Use it to build native fluency (Lab 6 in [../docs/05-advanced-labs.md](../docs/05-advanced-labs.md))
and to *see* exactly what pyvespa generates.

```
native-app/
├── services.xml          # topology: container + content clusters, the e5 embedder
├── schemas/doc.sd        # the schema + rank profiles (bm25 / semantic / fusion)
└── sample-docs.jsonl     # 10 documents to feed (embeddings generated on feed)
```

## Prerequisites
- Docker running.
- The Vespa CLI: `brew install vespa-cli`  (or `uv pip install vespacli`, then use `vespa`).

## 1. Start a clean Vespa container

```bash
# if the capstone container is still up, remove it first to free port 8080:
#   (from ../capstone) python teardown.py
docker run --detach --name vespa \
  -p 8080:8080 -p 19071:19071 \
  vespaengine/vespa
```

Wait ~15s for the config server (:19071) to come up. Check with:
```bash
curl -s http://localhost:19071/state/v1/health
```

## 2. Deploy the application package

```bash
cd native-app
vespa config set target local
vespa deploy --wait 300 .
```

`vespa deploy` uploads `services.xml` + `schemas/`, Vespa downloads the e5 ONNX model, and
the app becomes active. (First deploy is the slow one — model download.)

## 3. Feed the documents

```bash
vespa feed sample-docs.jsonl
```

Each line is one operation: `{"put": "id:tutorial:doc::1", "fields": {...}}`. Note the docs
have **no `embedding` field** — Vespa generates the 384-d vector from `title`+`body` via the
`embed e5` step in the schema, exactly like the Python version.

Confirm they're searchable:
```bash
vespa query 'yql=select * from sources * where true' hits=0
# look at totalCount in the JSON (should be 10)
```

## 4. Query — keyword, semantic, hybrid

The embedder runs at query time too, so you pass **text**, not a vector. The query tensor
`q` is set with `input.query(q)=embed(e5, "...")`.

```bash
# keyword (BM25)
vespa query \
  'yql=select id,title from sources * where userQuery()' \
  'query=fruits and vegetables for asthma' \
  ranking=bm25 hits=5

# semantic (vector ANN) — note the embed() in input.query(q)
vespa query \
  'yql=select id,title from sources * where ({targetHits:50}nearestNeighbor(embedding,q))' \
  'input.query(q)=embed(e5, "how does diet affect breathing problems")' \
  ranking=semantic hits=5

# hybrid (match either, fuse with RRF)
vespa query \
  'yql=select id,title from sources * where userQuery() or ({targetHits:50}nearestNeighbor(embedding,q))' \
  'query=how does diet affect breathing problems' \
  'input.query(q)=embed(e5, "how does diet affect breathing problems")' \
  ranking=fusion hits=5
```

Try a query where keyword alone fails — e.g. *"breathing problems"* (no doc contains those
exact words, but doc 1 is about asthma). `semantic`/`fusion` will still surface it; `bm25`
won't. That's the hybrid win, demonstrated natively.

## 5. Inspect like an operator

```bash
vespa query 'yql=select * from sources * where userQuery()' 'query=diabetes' \
  ranking=fusion hits=3 'presentation.format=json' | python3 -m json.tool   # see matchfeatures per hit

curl -s http://localhost:8080/state/v1/health
curl -s "http://localhost:8080/prometheus/v1/values?consumer=vespa" | head
```

The `fusion` profile exports `match-features` (bm25(title), bm25(body), closeness) on every
hit — open a result and read *why* it ranked where it did.

## 6. Clean up

```bash
docker rm -f vespa
```

---

### Map it back to Python
Open `schemas/doc.sd` next to `../capstone/app_package.py`. Every rank profile, the embedder
component, and the embedding field line up 1:1. Internalizing that mapping — Python you write
⇄ native artifact the engine runs — is the core of being "pro" with Vespa. Deeper notes:
[../docs/04-pro-deep-dive.md §1–§2](../docs/04-pro-deep-dive.md#1-application-package-internals).
