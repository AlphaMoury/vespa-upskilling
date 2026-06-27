# Vespa.ai in 48 Hours — Learning Pack

A self-contained kit to go from zero to "I can explain Vespa, build with it, and present it" in a weekend.

Built for: a Factored learning-path sprint, with a Monday TTO presentation.
Last verified against live Vespa docs / pyvespa: **2026-06-27** (Vespa 8.7xx, pyvespa 1.2.x).

---

## What's in here

```
vespa-upskilling/
├── README.md                  ← you are here
├── docs/
│   ├── 01-deep-dive.md        ← the main reading material (architecture + concepts, in depth)
│   ├── 02-study-plan-48h.md   ← an hour-by-hour plan for the weekend
│   └── 03-cheatsheet.md       ← one-page reference: schema, YQL, ranking, pyvespa
├── capstone/
│   ├── README.md              ← how to run the project, step by step
│   ├── requirements.txt
│   ├── setup.sh               ← one command to create the env (uses uv → Python 3.13)
│   ├── app_package.py         ← the Vespa application defined in Python
│   ├── 01_deploy_and_feed.py  ← deploy Vespa in Docker + feed a real dataset
│   ├── 02_search.py           ← compare keyword vs semantic vs hybrid search
│   ├── 03_evaluate.py         ← prove hybrid wins with an nDCG@10 leaderboard
│   ├── 04_search_ui.py        ← optional: a tiny web UI to demo live
│   └── teardown.py            ← stop/remove the container when done
└── slides/
    └── vespa-tto.html         ← the 2-slide deck (open in a browser, press F for fullscreen)
```

## How to use it (the fast path)

1. **Read first, ~3–4 h.** Open [docs/01-deep-dive.md](docs/01-deep-dive.md). It's written to be read top-to-bottom. Skim the boxes labelled *"In one sentence"* if you're short on time.
2. **Build, ~3–5 h.** Follow [capstone/README.md](capstone/README.md). You'll deploy a real Vespa instance in Docker, feed ~3,600 medical documents, and run keyword / semantic / hybrid search — then measure that hybrid actually ranks better.
3. **Present, ~1 h.** Open [slides/vespa-tto.html](slides/vespa-tto.html). Two slides, speaker notes included below them. Rehearse the 3-minute version in [docs/02-study-plan-48h.md](docs/02-study-plan-48h.md#the-3-minute-pitch-script).

If you only have a few hours total: read the **deep-dive intro + §2 + §6**, run the capstone's `01` and `02` scripts, and present the slides. That alone makes you credibly conversant.

## The one-paragraph version (so you're oriented before you start)

Vespa is an open-source engine, born inside Yahoo, that does in **one system** what most teams stitch together from three: a **keyword search engine** (like Elasticsearch), a **vector database** (like Pinecone), and a **machine-learned ranking service**. Its trick is running the ranking and ML inference **on the same nodes that store the data**, so it can rank with hundreds of signals over billions of constantly-updated documents at very low latency. You define an application declaratively (a *schema* + *ranking profiles*), deploy it, feed JSON, and query with **YQL** (a SQL-like language). The capstone here makes that concrete in about an hour of runtime.

## Prerequisites

- **Docker** (you have it). Vespa runs as a container; give Docker ~4–8 GB RAM.
- **Python 3.10–3.13.** Your system Python is 3.14, which pyvespa doesn't support yet — `setup.sh` uses `uv` to grab 3.13 automatically, no system changes.
- ~2 GB disk for the Vespa image, a few minutes of internet for the dataset + embedding model.

> Everything below is grounded in official sources (docs.vespa.ai, pyvespa docs, vespa.ai). Where a number is vendor-published or I couldn't fully verify it, the docs say so explicitly — keep that honesty in the presentation; it makes you more credible, not less.
