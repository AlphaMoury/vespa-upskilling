# The 48-Hour Plan

Today is **Saturday**. You present **Monday**. This plan assumes ~12–14 focused hours total across the weekend, with buffer. It's front-loaded: get the capstone *running* early (Saturday), because that's the thing that can surprise you with download/Docker time. Reading and slides are low-risk and can flex.

> **The golden rule for a 48h sprint:** depth in *one* path beats shallow coverage of all of it. Your path is **Python (pyvespa) → hybrid search → measured ranking quality**. Everything else (Java components, multi-node ops, Vespa Cloud) you only need to be able to *talk about*, not *do*.

---

## Saturday

### Block 1 — Orientation (1.5 h)
- Read **[01-deep-dive.md](01-deep-dive.md) §1–§7** (what Vespa is, architecture, schema, tensors, embeddings, ranking). Don't memorize — just build the mental model.
- Keep [03-cheatsheet.md](03-cheatsheet.md) open in a tab.
- ✅ Checkpoint: you can explain, out loud, "container vs content cluster" and "the three indexing modes."

### Block 2 — Get the capstone RUNNING (2.5 h) ← do this today, no matter what
- Follow **[../capstone/README.md](../capstone/README.md)** steps 0–2.
- Run `setup.sh`, then `01_deploy_and_feed.py`. This pulls the ~2 GB Vespa image, starts the container, downloads the e5 embedding model, and feeds ~3,600 docs. **The first run is the slow one** — let it churn while you read.
- ✅ Checkpoint: `01` finishes and reports docs fed. The container is up at http://localhost:8080.
- If anything breaks, the capstone README's Troubleshooting section covers the common cases (Docker RAM, port in use, dataset download).

### Block 3 — Play with search (2 h)
- Run `02_search.py`. Read its output: the *same query* under `bm25`, `semantic`, and `fusion` profiles, side by side. Notice where keyword wins, where semantic wins, where hybrid is best.
- Open `02_search.py` and change the queries to your own. Re-run. This is where intuition forms.
- Read **deep-dive §8 (hybrid)** again now that you've seen it move.
- ✅ Checkpoint: you can point at a specific query where hybrid beat both single methods, and explain *why*.

### Block 4 (optional, if energized) — The proof (1 h)
- Run `03_evaluate.py` → the **nDCG@10 leaderboard**. This is your headline demo result. Screenshot it.
- ✅ Checkpoint: you have a number showing hybrid > bm25 and hybrid > semantic.

**End of Saturday target:** capstone runs end-to-end; you've seen hybrid win on a metric. The hard part is now behind you.

---

## Sunday

### Block 5 — Fill in the concepts (2 h)
- Read **deep-dive §9–§13** (YQL, RAG, who-uses-it, trade-offs, deployment).
- Skim the official **hybrid-search tutorial** (https://docs.vespa.ai/en/tutorials/hybrid-search.html) to see the same thing in raw `.sd`/YQL form — confirm you can *read* the native syntax, not just the Python.
- Look at the deployed app's generated files: in `capstone/`, run `python -c "from app_package import package; package.to_files('generated_app'); print('see generated_app/')"` and read the `schemas/doc.sd` and `services.xml` it produced. Connecting the Python you wrote to the XML/`.sd` it generated is a big "click" moment.
- ✅ Checkpoint: you can read a `.sd` schema and a YQL query unaided.

### Block 6 — Make it yours (2 h, the part that impresses a TTO)
Pick **one** small extension so the capstone is *yours*, not a copied tutorial:
- **(easiest)** Add a **filter**: a hybrid query restricted with `where ... and range(...)` or `title contains "..."`. Shows you understand structured + semantic together.
- **(medium)** Add a **new rank profile** — e.g. a linear hybrid `0.3*bm25 + 0.7*closeness` — and compare its nDCG against `fusion` in `03_evaluate.py`. Shows you can *tune relevance*, the core Vespa skill.
- **(medium)** Demo a **real-time partial update**: bump an attribute on a doc and show its ranking change on the next query (the freshness story). (You'd add a numeric attribute field and reference it in a rank profile.)
- **(stretch)** Wire up **RAG**: feed top hits into an LLM call (even client-side with the Anthropic/OpenAI SDK) to generate an answer with citations. Honest framing: "Vespa has a built-in RAGSearcher; here I did the generation client-side to keep it simple."
- ✅ Checkpoint: one extension works and you can explain the Vespa feature it exercises.

### Block 7 — Build the slides & rehearse (2 h)
- Open **[../slides/vespa-tto.html](../slides/vespa-tto.html)** in a browser. The two slides are done; your job is to **make them true to your demo** — drop in *your* nDCG numbers and *your* example query screenshot if you like.
- Read the speaker notes under each slide.
- Rehearse the **3-minute pitch** (below) out loud 3×. Time yourself.
- ✅ Checkpoint: you can deliver the pitch in ≤3 min without reading.

### Block 8 — Buffer / polish (1 h)
- Re-run the whole capstone clean (`teardown.py` then `01`→`02`→`03`) so you know it works from scratch for any live demo.
- Write down 3 questions you think the TTO will ask and your answers (see "Likely questions" below).

---

## The 3-minute pitch script

> Use this as the spine of your presentation. It maps to the two slides.

**(0:00 – The problem, 30s)** "Most teams that build search or RAG today run three systems: a keyword engine like Elasticsearch, a vector database like Pinecone, and a separate model service to re-rank. That means two copies of the data, ranking that happens far from the data over the network, and a freshness lag. It's a lot of glue."

**(0:30 – What Vespa is, 30s)** "Vespa is one open-source engine — built inside Yahoo, now independent — that does all three in a single system. Its key idea is that it runs the ranking and the ML models *on the same nodes that store the data*. So keyword, vector, structured filters, and a learned model are all evaluated in one query, right where the data lives."

**(1:00 – Why it matters, 30s)** "That unlocks two things the three-system stack struggles with: ranking with hundreds of signals at scale and low latency, and *real-time* updates — a price or a click changes the ranking on the very next query, no reindexing. It's the engine behind Perplexity's retrieval, Spotify's podcast search, and Vinted's marketplace."

**(1:30 – What I built, 60s)** "Over the weekend I built a hybrid-search app on it in Python. I deployed Vespa in Docker, fed ~3,600 medical documents, and let Vespa generate the embeddings itself. Then I ran the same queries three ways — keyword only, semantic only, and hybrid — and measured ranking quality with nDCG. Hybrid won: [your number] versus [bm25] and [semantic]. Here's a query where keyword search missed the synonym, vector search missed the exact term, and hybrid caught both." *(show the example)*

**(2:30 – Where it fits for us, 30s)** "For our clients, this is the consolidation play: when someone outgrows 'Elasticsearch plus a vector DB plus a reranker,' Vespa replaces all three. The catch is honest — it has a steeper learning curve and custom logic is in Java — but that complexity is exactly where a services team like ours adds value: schema design and relevance tuning. I'd suggest a small follow-up spike on [a real client use case]."

---

## Likely questions from the TTO (prep your answers)

1. **"How is this different from Elasticsearch's vector support?"** → Elastic bolted vectors onto a keyword engine; ranking and ML still happen separately and updates lag (immutable segments). Vespa was designed around unified retrieval + ML ranking *next to the data* with mutable, instantly-searchable writes. Vespa's own benchmark claims multiples-higher throughput per core (vendor-run — cite it as such).
2. **"Why not just use Pinecone/Weaviate?"** → Great for simple, managed, vector-first needs. Vespa wins when you also need strong lexical search, custom multi-phase ML ranking, real-time updates, and billion-scale — in one system.
3. **"What's the cost of adopting it?"** → Steeper learning curve (YQL, schemas, ranking), heavier ops than a hosted vector DB, custom components are JVM/Java, smaller talent pool. Vespa Cloud removes the infra burden. Budget weeks to production.
4. **"Can it do RAG / LLMs?"** → Yes — built-in `RAGSearcher` retrieves (hybrid + ML rank) and calls an LLM (external API or local/GPU), streaming the answer. The `rag-blueprint` sample is "the architecture that powers Perplexity."
5. **"Did you actually run it, or is this slides?"** → "Running locally in Docker right now — want to see a live query?" *(Have the container up.)*

---

## If you fall behind

Minimum viable version that still lands well:
1. Read deep-dive **§1, §2, §7, §8, §12**.
2. Run capstone **`01` + `02`** only (skip eval and extensions).
3. Present the slides with the pitch script; be honest that the metric demo is "next step."

That's still a genuine, hands-on understanding — far more than reading marketing pages.

---

## After the sprint — going pro

The weekend gets you *conversant and demo-ready*. To become genuinely **fluent in the framework** (the "very technical, very deep" goal), continue with the Pro track — it's built to be worked over the following 1–2 weeks, not crammed:

- **[04-pro-deep-dive.md](04-pro-deep-dive.md)** — the deep reference: ranking mastery, ONNX/GBDT, learning-to-rank, ANN tuning, perf/sizing, Java components, ops, and a **fluency checklist**.
- **[05-advanced-labs.md](05-advanced-labs.md)** — eight hands-on labs that turn the capstone into real skill. Aim for one or two per sitting; tick the fluency checklist as you go.
- **[../native-app/](../native-app/)** — deploy the same app from raw `.sd`/`services.xml` with the `vespa` CLI.
- **[../pro-java-searcher/](../pro-java-searcher/)** — write a Java component that runs inside Vespa.

A good "I'm fluent now" milestone: complete the **capstone-of-the-capstone** in the labs (a mini learning-to-rank: export features → train an XGBoost model → deploy it to a `second-phase` → beat your hand-tuned hybrid on nDCG). That single exercise touches the entire stack.
