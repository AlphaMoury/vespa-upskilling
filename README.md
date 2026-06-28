# Vespa.ai in 48 Hours — Learning Pack

A self-contained kit to go from zero to "I can explain Vespa, build with it, and present it" in a weekend.

Built for: a Factored learning-path sprint, with a Monday TTO presentation.
Last verified against live Vespa docs / pyvespa: **2026-06-27** (Vespa 8.7xx, pyvespa 1.2.x).

---

## What's in here

```
vespa-upskilling/
├── README.md                  ← you are here
├── index.html                 ← landing page (served by GitHub Pages)
├── docs/
│   ├── 01-deep-dive.md        ← the main reading material (architecture + concepts, in depth)
│   ├── 02-study-plan-48h.md   ← an hour-by-hour plan for the weekend
│   ├── 03-cheatsheet.md       ← one-page reference: schema, YQL, ranking, pyvespa
│   ├── 04-pro-deep-dive.md    ← ★ PRO: internals, ranking/ML mastery, perf, extend, ops, fluency checklist
│   └── 05-advanced-labs.md    ← ★ PRO: 8 hands-on labs that extend the capstone into real fluency
├── capstone/                  ← the Python (pyvespa) project
│   ├── README.md              ← how to run the project, step by step
│   ├── setup.sh               ← one command to create the env (uses uv → Python 3.13)
│   ├── app_package.py         ← the Vespa application defined in Python
│   ├── 01_deploy_and_feed.py  ← deploy Vespa in Docker + feed a real dataset
│   ├── 02_search.py           ← compare keyword vs semantic vs hybrid search
│   ├── 03_evaluate.py         ← prove hybrid wins with an nDCG@10 leaderboard
│   ├── 04_search_ui.py        ← optional: a tiny web UI to demo live
│   └── teardown.py            ← stop/remove the container when done
├── native-app/                ← ★ PRO: the SAME app as raw services.xml + .sd, deployed via the `vespa` CLI
│   ├── services.xml · schemas/doc.sd · sample-docs.jsonl · README.md
├── pro-java-searcher/         ← ★ PRO: a buildable Java Searcher (the engine-extension model)
│   ├── pom.xml · src/… · README.md
└── slides/
    └── vespa-tto.html         ← the 2-slide deck (open in a browser, press F for fullscreen)
```

## How to use it (the fast path)

1. **Read first, ~3–4 h.** Open [docs/01-deep-dive.md](docs/01-deep-dive.md). It's written to be read top-to-bottom. Skim the boxes labelled *"In one sentence"* if you're short on time.
2. **Build, ~3–5 h.** Follow [capstone/README.md](capstone/README.md). You'll deploy a real Vespa instance in Docker, feed ~3,600 medical documents, and run keyword / semantic / hybrid search — then measure that hybrid actually ranks better.
3. **Present, ~1 h.** Open [slides/vespa-tto.html](slides/vespa-tto.html). Two slides, speaker notes included below them. Rehearse the 3-minute version in [docs/02-study-plan-48h.md](docs/02-study-plan-48h.md#the-3-minute-pitch-script).

If you only have a few hours total: read the **deep-dive intro + §2 + §6**, run the capstone's `01` and `02` scripts, and present the slides. That alone makes you credibly conversant.

### Going pro (after the basics — for real technical fluency)

The first three docs make you *conversant*. To become *fluent in the framework* — able to design schemas, tune ranking, extend the engine, and operate it — work the Pro track:

4. **[docs/04-pro-deep-dive.md](docs/04-pro-deep-dive.md)** — the deep technical reference: application-package internals, ranking mastery (phases, ONNX/GBDT, learning-to-rank), ANN tuning, the query stack, performance & sizing, Java components, ops, and a **fluency checklist** to measure yourself against.
5. **[docs/05-advanced-labs.md](docs/05-advanced-labs.md)** — eight hands-on labs that extend your capstone (export `match-features`, A/B rank profiles on nDCG, tune HNSW, real-time updates, native CLI deploy, a Java searcher, RAG). Fluency is built here, by doing.
6. **[native-app/](native-app/)** — the *same* app written as raw `services.xml` + `.sd`, deployed with the `vespa` CLI. Read it next to `capstone/app_package.py` to see exactly what pyvespa generates. (Python stays your driver; this is so you can read/write the native form when it matters.)
7. **[pro-java-searcher/](pro-java-searcher/)** — a buildable Java Searcher: the real way to extend Vespa with custom logic.

## The one-paragraph version (so you're oriented before you start)

Vespa is an open-source engine, born inside Yahoo, that does in **one system** what most teams stitch together from three: a **keyword search engine** (like Elasticsearch), a **vector database** (like Pinecone), and a **machine-learned ranking service**. Its trick is running the ranking and ML inference **on the same nodes that store the data**, so it can rank with hundreds of signals over billions of constantly-updated documents at very low latency. You define an application declaratively (a *schema* + *ranking profiles*), deploy it, feed JSON, and query with **YQL** (a SQL-like language). The capstone here makes that concrete in about an hour of runtime.

## Prerequisites

- **Docker** (you have it). Vespa runs as a container; give Docker ~4–8 GB RAM.
- **Python 3.10–3.13.** Your system Python is 3.14, which pyvespa doesn't support yet — `setup.sh` uses `uv` to grab 3.13 automatically, no system changes.
- ~2 GB disk for the Vespa image, a few minutes of internet for the dataset + embedding model.

> Everything below is grounded in official sources (docs.vespa.ai, pyvespa docs, vespa.ai). Where a number is vendor-published or I couldn't fully verify it, the docs say so explicitly — keep that honesty in the presentation; it makes you more credible, not less.
