"""
Step 1: Deploy Vespa locally in Docker, then feed it documents.

By default it feeds BeIR/NFCorpus (~3,600 docs). To GO BIG, point it at a larger
BeIR dataset and/or raise the cap — Vespa generates an e5 embedding for every doc on
ingest, so feed throughput (docs/sec) is the thing to watch.

    Usage:
      python 01_deploy_and_feed.py                       # NFCorpus, all ~3,600 docs
      DATASET=fiqa python 01_deploy_and_feed.py          # ~57k financial docs
      DATASET=trec-covid python 01_deploy_and_feed.py    # ~171k medical docs  (lots!)
      DATASET=quora MAX_DOCS=300000 python 01_deploy_and_feed.py   # ~523k, capped

    Knobs (env vars):
      DATASET    short BeIR name: nfcorpus | fiqa | trec-covid | quora | scidocs | scifact ...
      MAX_DOCS   cap the number of docs fed (default: all)
      WORKERS    feed concurrency (default 12) — higher saturates more CPU cores for embedding

Re-running is safe; it reuses the running container. To start clean: `python teardown.py` first.
"""

import os
import time

from vespa.deployment import VespaDocker
from vespa.io import VespaResponse

from app_package import package, SCHEMA, NAMESPACE

DATASET = os.environ.get("DATASET", "nfcorpus").strip()
MAX_DOCS = int(os.environ["MAX_DOCS"]) if os.environ.get("MAX_DOCS") else None
WORKERS = int(os.environ.get("WORKERS", "12"))


def main():
    print(f">> Dataset: BeIR/{DATASET}   max_docs: {MAX_DOCS or 'ALL'}   feed workers: {WORKERS}")
    print(">> Deploying Vespa in Docker (first run pulls the image + embedding model)...")
    t0 = time.time()
    vespa_docker = VespaDocker(port=8080)
    app = vespa_docker.deploy(application_package=package)
    print(f">> Deployed and ready in {time.time() - t0:.0f}s. Endpoint: http://localhost:8080")

    print(f">> Streaming the {DATASET} corpus from HuggingFace...")
    from datasets import load_dataset

    dataset = load_dataset(f"BeIR/{DATASET}", "corpus", split="corpus", streaming=True)

    def feed_docs():
        n = 0
        for x in dataset:
            if MAX_DOCS is not None and n >= MAX_DOCS:
                break
            n += 1
            yield {
                "id": x["_id"],
                "fields": {
                    "id": x["_id"],
                    "title": x.get("title") or "",
                    "body": x.get("text") or "",
                },
            }

    state = {"fed": 0, "errors": 0, "t_last": time.time(), "n_last": 0}

    def callback(response: VespaResponse, doc_id: str):
        if response.is_successful():
            state["fed"] += 1
            if state["fed"] % 1000 == 0:
                now = time.time()
                rate = (state["fed"] - state["n_last"]) / max(now - state["t_last"], 1e-6)
                print(f"   ...fed {state['fed']:>7,} docs   ({rate:,.0f} docs/sec)")
                state["t_last"], state["n_last"] = now, state["fed"]
        else:
            state["errors"] += 1
            if state["errors"] <= 5:
                print(f"   ! error feeding {doc_id}: {response.get_json()}")

    print(">> Feeding (Vespa embeds each doc as it arrives — watch the docs/sec)...")
    t1 = time.time()
    app.feed_iterable(
        feed_docs(),
        schema=SCHEMA,
        namespace=NAMESPACE,
        callback=callback,
        max_queue_size=4000,
        max_workers=WORKERS,
        max_connections=WORKERS,
    )
    dt = time.time() - t1

    total = None
    try:
        r = app.query(yql="select * from sources * where true", hits=0)
        total = r.json.get("root", {}).get("fields", {}).get("totalCount")
    except Exception as e:  # noqa: BLE001
        print(f"   (could not read totalCount: {e})")

    avg = state["fed"] / dt if dt else 0
    print("\n========================================")
    print(f"  Fed {state['fed']:,} docs in {dt:.0f}s  (avg {avg:,.0f} docs/sec, {state['errors']} errors).")
    if total is not None:
        print(f"  Vespa reports {total:,} searchable documents.")
    print("  Watch it live:   python scale_watch.py")
    print("  Then:            python 02_search.py   /   python 03_evaluate.py")
    print("========================================")


if __name__ == "__main__":
    main()
