# Advanced Labs — Build Real Fluency

Reading makes you *conversant*; these labs make you *fluent*. Each one is a focused, hands-on extension of your [capstone](../capstone/README.md) that drills one pro skill. Do them roughly in order — they escalate. Every lab states the **skill**, the **steps**, and a **done-when** check.

Prereq: the capstone is deployed and fed (`capstone/01_deploy_and_feed.py` has run; Vespa is up on :8080). Work inside the activated `.venv`.

> Tip: keep [04-pro-deep-dive.md](04-pro-deep-dive.md) open — each lab points at the section it exercises.

---

## Lab 0 — Read what pyvespa generates (15 min) · skill: native fluency

You can't be "pro" through a wrapper you can't see through.

```bash
cd capstone
python -c "from app_package import package; package.to_files('generated_app'); print('open generated_app/')"
```
Open `generated_app/schemas/doc.sd` and `generated_app/services.xml`. Map every Python object to its native output:
- `Field(name="embedding", ... ann=HNSW(...))` → the `field embedding { indexing: … | embed e5 | attribute | index  attribute { distance-metric } index { hnsw {…} } }` block.
- `Component(id="e5", type="hugging-face-embedder")` → the `<component>` in `services.xml`.
- `RankProfile("fusion", global_phase=…)` → the `rank-profile fusion { … global-phase { reciprocal_rank_fusion(…) } }`.

**Done when:** you can point at any line in `doc.sd` and say which Python produced it, and vice-versa. (See [§1–§2](04-pro-deep-dive.md#1-application-package-internals).)

---

## Lab 1 — Export ranking features with `match-features` (30 min) · skill: relevance engineering

The foundation of learning-to-rank: see the raw signals behind every hit.

1. In `capstone/app_package.py`, add a `match_features` list to the `fusion` profile (and a couple of helper functions). Edit the `fusion` `RankProfile(...)` to include:
   ```python
   from vespa.package import RankProfile, Function, GlobalPhaseRanking
   RankProfile(
       name="fusion",
       inherits="bm25",
       inputs=[("query(q)", "tensor<float>(x[384])")],
       first_phase="closeness(field, embedding)",
       global_phase=GlobalPhaseRanking(
           expression="reciprocal_rank_fusion(bm25sum, closeness(field, embedding))",
           rerank_count=1000,
       ),
       match_features=["bm25(title)", "bm25(body)", "closeness(field, embedding)"],
   )
   ```
2. Redeploy + re-feed: `python teardown.py && python 01_deploy_and_feed.py` (or just redeploy if your pyvespa supports schema-only updates).
3. Query and read the features:
   ```python
   from app_package import connect_local, embed_body
   app = connect_local()
   r = app.query(yql="select * from sources * where userQuery() or ({targetHits:100}nearestNeighbor(embedding,q)) limit 3",
                 query="vitamin D and bone health", ranking="fusion",
                 body=embed_body("vitamin D and bone health"))
   for h in r.hits:
       print(h["relevance"], h["fields"].get("matchfeatures"))
   ```

**Done when:** each hit prints its `matchfeatures` dict (bm25(title), bm25(body), closeness). You now have the exact inputs an LTR model would train on. (See [§4–§5](04-pro-deep-dive.md#4-ranking-mastery).)

---

## Lab 2 — Add a rank profile and A/B it on nDCG (45 min) · skill: rank tuning + measurement

Hypothesis: a tuned linear hybrid beats RRF on this corpus. Test it like a pro — with a number.

1. Add a normalized linear hybrid to `app_package.py` (global-phase so normalization is valid):
   ```python
   RankProfile(
       name="linear",
       inherits="bm25",
       inputs=[("query(q)", "tensor<float>(x[384])")],
       first_phase="closeness(field, embedding)",
       global_phase=GlobalPhaseRanking(
           expression="normalize_linear(bm25sum) + normalize_linear(closeness(field, embedding))",
           rerank_count=1000,
       ),
   )
   ```
2. Redeploy. Add `"linear"` to the `MODES` list in `03_evaluate.py` and to `app_package.py`'s `search_ids` (add an `elif mode == "linear"` branch identical to `fusion` but `ranking="linear"`).
3. Run `python 03_evaluate.py` and read the leaderboard with four rows now.

**Done when:** you have nDCG@10 for `bm25`, `semantic`, `fusion`, and `linear`, and can argue from the numbers which combiner wins *on this data* (and why one might win on other data). That argument-from-measurement is the skill. (See [§10](04-pro-deep-dive.md#10-relevance-engineering--evaluation).)

---

## Lab 3 — Tune ANN recall vs latency (30 min) · skill: vector-search tuning

1. Pick one query. Run it semantic-only with exact NN and with HNSW, varying `targetHits`:
   ```python
   # exact brute force (ground truth):
   yql_exact = "select id from sources * where ({targetHits:100,approximate:false}nearestNeighbor(embedding,q))"
   # approximate HNSW at different targetHits: 10, 50, 200
   ```
   Compare the returned id sets — recall = |HNSW ∩ exact| / |exact|. Time each (`response.json['timing']`).
2. Note how higher `targetHits` trades latency for recall.
3. Bonus: change the embedding field's `distance-metric` to `euclidean`, redeploy, re-run a query, and watch relevance degrade — proof that the metric must match the (normalized) model.

**Done when:** you can state, for this corpus, the `targetHits` where approximate recall plateaus near exact — and you've *seen* a wrong distance-metric break results. (See [§3](04-pro-deep-dive.md#3-vector-search--embeddings-tuned).)

---

## Lab 4 — Grouping / facets (20 min) · skill: aggregation

NFCorpus is flat, so first add a cheap synthetic attribute to group on, or group by a derived bucket. Simplest: add an attribute and group.

1. Add `Field(name="body_len", type="int", indexing=["attribute","summary"])` and populate it in `01`'s feed map (`"body_len": len(x["text"] or "")`). Redeploy + re-feed.
2. Query with grouping:
   ```python
   app.query(yql="select * from sources * where userQuery() | "
                 "all(group(fixedwidth(body_len,500)) max(5) order(-count()) each(output(count())))",
             query="diet", hits=0)
   ```
**Done when:** you get back buckets of documents by length with counts — i.e., you can build a faceted UI. (See [§6](04-pro-deep-dive.md#6-the-query-stack).)

---

## Lab 5 — Real-time partial updates change ranking (30 min) · skill: the freshness differentiator

This is the demo that separates Vespa from a static vector index.

1. Add a mutable signal: `Field(name="boost", type="float", indexing=["attribute","summary"])` and a rank profile that uses it: `first_phase="closeness(field, embedding) * (1 + attribute(boost))"`. Redeploy.
2. Feed a doc with `boost: 0`. Query, note its position.
3. Partial-update just that attribute — no reindex:
   ```python
   app.update_data(schema="doc", data_id="MED-123", fields={"boost": 5.0})
   ```
4. Re-run the same query immediately.

**Done when:** the doc jumps in the ranking on the very next query, with no re-feed/re-embed. You've demonstrated in-place, instantly-searchable updates — the recommendation/personalization story. (See [§7](04-pro-deep-dive.md#7-performance--sizing).)

---

## Lab 6 — Deploy the SAME app natively with the CLI (40 min) · skill: native ops

Now leave Python entirely and operate Vespa like its engineers do.

1. `python teardown.py` (free port 8080). Start a clean container:
   ```bash
   docker run --detach --name vespa -p 8080:8080 -p 19071:19071 vespaengine/vespa
   ```
2. Install the CLI (`brew install vespa-cli` or `uv pip install vespacli`), then:
   ```bash
   cd ../native-app
   vespa config set target local
   vespa deploy --wait 300 .
   vespa feed sample-docs.jsonl
   vespa query 'yql=select * from sources * where userQuery() or ({targetHits:50}nearestNeighbor(embedding,q))' \
               'query=how vegetables affect asthma' \
               'input.query(q)=embed(e5, "how vegetables affect asthma")' \
               ranking=fusion hits=5
   curl -s http://localhost:8080/state/v1/health
   ```
**Done when:** you deployed from raw `.sd`/`services.xml`, fed JSONL, ran a hybrid query, and checked health — no pyvespa involved. See [../native-app/README.md](../native-app/README.md). (See [§9](04-pro-deep-dive.md#9-operating-vespa).)

---

## Lab 7 — Write a Java Searcher (60–90 min, optional) · skill: extending the engine

Requires JDK 17+ and Maven. This is the real extension model.

1. `cd ../pro-java-searcher`, read the README, `mvn clean package`.
2. `vespa deploy --wait 300 target/application` (the build assembles `services.xml` + your bundle).
3. Query and see the searcher's effect (it defaults the ranking profile to `fusion` and tags results).

**Done when:** your own Java class runs inside Vespa's query path and changes behavior. You now know how to add arbitrary logic — query rewriting, blending, external calls, custom endpoints. (See [§8](04-pro-deep-dive.md#8-extending-vespa-with-java-components) and [../pro-java-searcher/](../pro-java-searcher/).)

---

## Lab 8 — RAG on top (60 min, stretch) · skill: end-to-end retrieval-augmented generation

Turn retrieval into answers.

1. Hybrid-retrieve the top 5 chunks for a question (reuse `search_ids`, but `select id, title, body`).
2. Build a prompt: system instruction + the retrieved passages as context + the question.
3. Call an LLM (client-side is fine to start — Anthropic/OpenAI SDK) and print the answer **with citations** back to the source doc ids.
4. Honest framing for your write-up: *"Vespa has a built-in `RAGSearcher` that does this inside the container with streaming; I did generation client-side first to keep the moving parts visible, then can move it in-engine."*

**Done when:** a question produces a grounded, cited answer whose sources you can trace to retrieved documents. (See deep-dive [§10](01-deep-dive.md#10-rag-with-vespa-retrieval-augmented-generation) and pro [§5](04-pro-deep-dive.md#5-ml-in-ranking-onnx-gbdt-and-the-ltr-workflow).)

---

## Capstone-of-the-capstone (if you want one hard thing)

Combine Labs 1–2 into a **mini learning-to-rank**: export `match-features` over the NFCorpus training queries, label rows with the qrels, train a tiny XGBoost model in Python, dump it to `models/ltr.json`, reference it in a `second-phase` rank profile, redeploy, and show nDCG@10 beating your hand-tuned hybrid. That single exercise touches schema, ranking, features, ML, deployment, and evaluation — the whole pro loop in one artifact. ([§5](04-pro-deep-dive.md#5-ml-in-ranking-onnx-gbdt-and-the-ltr-workflow))

---

### Track your fluency
Tick the [fluency checklist](04-pro-deep-dive.md#11-the-fluency-checklist) as each lab lands. When they're all checked, you're not "learning Vespa" anymore — you're using it.
