# Capstone: Hybrid Search on Vespa, with Measured Ranking Quality

**What you'll build:** a real search application on Vespa that indexes ~3,600 medical
documents and answers queries three ways — pure keyword (BM25), pure semantic (vector),
and **hybrid** — then *measures* that hybrid ranks best using nDCG@10 (a standard search
metric with human relevance judgments).

**Why this one:** it's the single example that exercises most of Vespa's distinctive
surface — schema design, Vespa-generated embeddings, HNSW vector search, BM25, and
multi-phase / global-phase ranking — and it ends with a **number you can put on a slide**.
It mirrors the official Vespa hybrid-search tutorial, driven from Python.

**What it showcases (say these words in your presentation):** one engine doing lexical +
vector + ML-style ranking in a single query; embeddings generated *inside* Vespa; and
relevance you can quantify and tune.

---

## 0. Prerequisites (2 min)

- **Docker running**, with ~6–8 GB RAM allocated (Docker Desktop → Settings → Resources).
- **`uv`** installed (you have it). It will fetch Python 3.13 for us, because pyvespa
  doesn't support your system's Python 3.14 yet.
- A few minutes of internet (Vespa image ~2 GB, embedding model, dataset).

## 1. Set up the environment (5 min, mostly download)

```bash
cd capstone
bash setup.sh
source .venv/bin/activate
```

`setup.sh` creates `.venv/` on Python 3.13 and installs `pyvespa`, `datasets`, etc.

## 2. Deploy Vespa + feed the data (10–20 min first run)

```bash
python 01_deploy_and_feed.py
```

This pulls the Vespa image, starts the container on `localhost:8080`, uploads the app,
downloads the e5 embedding model, and feeds the corpus — **Vespa computes an embedding
for every document as it arrives.** First run is the slow one; grab a coffee and read the
[deep dive](../docs/01-deep-dive.md). When it prints the doc count, you're live.

> Want a faster first pass? `MAX_DOCS=500 python 01_deploy_and_feed.py` feeds only 500 docs.

## 3. Compare the three search methods (2 min)

```bash
python 02_search.py
```

Prints the same queries under `bm25`, `semantic`, and `fusion`. Read where each wins.
**Edit the `QUERIES` list in `02_search.py` to your own and re-run** — this is where the
intuition lands.

## 4. Prove hybrid wins (5 min) ← your headline result

```bash
python 03_evaluate.py
```

Runs 50 judged queries under each profile and prints an **nDCG@10 leaderboard**. Expect
`fusion` on top. **Screenshot this table** for the presentation.

## 5. (Optional) Live web UI for the demo

```bash
uv pip install streamlit
streamlit run 04_search_ui.py
```

A three-column page: type a query, see keyword vs semantic vs hybrid side by side. Great
for a live moment in the room.

## 6. Clean up

```bash
python teardown.py        # stop + remove the container
```

---

## What each file is

| File | Role |
|------|------|
| `app_package.py` | **The app, defined in Python**: schema, fields, the e5 embedder, and the 3 rank profiles. Start reading here. |
| `01_deploy_and_feed.py` | Deploy to Docker; stream + feed NFCorpus (Vespa embeds each doc). |
| `02_search.py` | Side-by-side keyword / semantic / hybrid results. |
| `03_evaluate.py` | nDCG@10 leaderboard — the measured proof. |
| `04_search_ui.py` | Optional Streamlit UI for a live demo. |
| `teardown.py` | Stop/remove the container. |

## See the native Vespa form (worth 5 minutes)

The Python in `app_package.py` *generates* a standard Vespa application package. Look at it:

```bash
python -c "from app_package import package; package.to_files('generated_app'); print('open generated_app/')"
```

Open `generated_app/schemas/doc.sd` (the schema) and `generated_app/services.xml` (the
topology). Connecting the Python you wrote to the `.sd`/XML it produced is a big "click"
moment — and it proves you understand the real thing, not just a wrapper.

---

## Make it yours (pick one — this is what impresses a TTO)

See [../docs/02-study-plan-48h.md](../docs/02-study-plan-48h.md#block-6--make-it-yours-2-h-the-part-that-impresses-a-tto). Quick options:
- **Filtered hybrid:** add `and title contains "diet"` to a query in `02_search.py`.
- **A 4th rank profile:** add a linear hybrid (e.g. `0.3*bm25sum + 0.7*closeness(field, embedding)`)
  in `app_package.py`, redeploy (`python teardown.py && python 01_deploy_and_feed.py`), and add it
  to `MODES` in `03_evaluate.py` to compare against RRF.
- **RAG stretch:** take the top hits from a hybrid query and feed them to an LLM to generate a
  cited answer. (Vespa has a built-in `RAGSearcher`; doing it client-side first is a fine,
  honest simplification to mention.)

---

## Going big (scale it up)

The default run is ~3,600 docs so you get a result fast. To actually *feel* the platform,
point it at a bigger BeIR dataset — same code, just env vars. Vespa embeds every doc on
ingest (on CPU), so **feed throughput (docs/sec) is the thing to watch**, not storage.

```bash
# pick a bigger corpus; DATASET must match in BOTH 01 and 03
DATASET=fiqa        python 01_deploy_and_feed.py      # ~57k  financial docs
DATASET=trec-covid  python 01_deploy_and_feed.py      # ~171k medical docs (lots!)
DATASET=quora MAX_DOCS=300000 WORKERS=16 python 01_deploy_and_feed.py   # ~523k, capped

# in a SECOND terminal, watch the document count climb live:
python scale_watch.py

# evaluate on the same dataset (qrels come from BeIR/<DATASET>-qrels):
DATASET=trec-covid python 03_evaluate.py
```

Knobs: `DATASET` (nfcorpus | fiqa | trec-covid | quora | scidocs | scifact …), `MAX_DOCS`
(cap the count), `WORKERS` (feed concurrency — higher saturates more CPU cores for embedding).

What to look at while it runs — this is the "see the whole thing" part:
- `scale_watch.py` — docs/sec and the live count.
- `curl -s "http://localhost:8080/prometheus/v1/values?consumer=vespa" | grep -i memory` — memory growth.
- `curl -s http://localhost:8080/state/v1/health` — node health.
- Re-run `02_search.py` mid-feed — Vespa serves queries *while* indexing (real-time).

Honest scale note: 384-d vectors are ~1.5 KB each, so hundreds of thousands of docs fit in
8 GB; the CPU embedder is the limiter. Give Docker Desktop **8 GB+** for the big datasets.
True billion-scale is a multi-node cluster / Vespa Cloud — that's the whole point of the engine,
and a good line for the TTO: *"this same app scales horizontally; the laptop just shows it works."*

## Troubleshooting

| Symptom | Fix |
|---|---|
| `setup.sh`: "uv not found" | Install uv: `curl -LsSf https://astral.sh/uv/install.sh \| sh`, restart shell. |
| Deploy hangs / OOM / container exits | Give Docker more RAM (6–8 GB). `python teardown.py` then retry. |
| `port 8080 already in use` | Something else (or an old Vespa) holds 8080. `python teardown.py`, or `docker ps` and stop it. |
| `01` fails downloading the model | Transient network; re-run. The model is fetched from data.vespa-cloud.com on deploy. |
| `datasets` can't load NFCorpus | Check internet; or `pip install -U datasets`. The configs are `"corpus"` and `"queries"`; qrels live in `BeIR/nfcorpus-qrels`. |
| `02`/`03`: connection refused | Vespa isn't up. Run `01` first; confirm `docker ps` shows a `vespaengine/vespa` container. |
| Querying feels slow the first time | The query embedder warms up on first call; subsequent queries are fast. |
| pyvespa kwarg / import error | API moves fast. `pip show pyvespa` to check version; `help(Field)` / `help(app.feed_iterable)` is ground truth. The code targets pyvespa 1.x. |

> Honesty note for the presentation: this runs on a laptop with a few thousand docs. The same
> schema and queries scale to billions of documents across many nodes — that's the point of
> Vespa — but don't claim laptop-scale numbers as production benchmarks.
