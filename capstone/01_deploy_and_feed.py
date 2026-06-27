"""
Step 1: Deploy Vespa locally in Docker, then feed it ~3,600 medical documents.

What happens here (this is the slow, one-time step — let it run while you read):
  1. pyvespa pulls the `vespaengine/vespa` image and starts a container on :8080.
  2. It uploads our application package (schema + rank profiles + the e5 embedder).
  3. Vespa downloads the e5-small-v2 ONNX model the first time it deploys.
  4. We stream the BeIR/NFCorpus corpus from HuggingFace and feed it. Vespa generates
     a 384-d embedding for every document ON THE WAY IN (you never compute a vector).

Re-running is safe; it reuses the running container. To start clean: `python teardown.py` first.

    Usage:  python 01_deploy_and_feed.py
            MAX_DOCS=500 python 01_deploy_and_feed.py     # feed fewer docs (faster)
"""

import os
import time

from vespa.deployment import VespaDocker
from vespa.io import VespaResponse

from app_package import package, SCHEMA, NAMESPACE

MAX_DOCS = int(os.environ["MAX_DOCS"]) if os.environ.get("MAX_DOCS") else None


def main():
    print(">> Deploying Vespa in Docker (first run pulls the image + embedding model)...")
    t0 = time.time()
    vespa_docker = VespaDocker(port=8080)
    app = vespa_docker.deploy(application_package=package)
    print(f">> Deployed and ready in {time.time() - t0:.0f}s. Endpoint: http://localhost:8080")

    print(">> Loading the NFCorpus corpus (streaming from HuggingFace)...")
    from datasets import load_dataset

    dataset = load_dataset("BeIR/nfcorpus", "corpus", split="corpus", streaming=True)

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
                    "title": x["title"] or "",
                    "body": x["text"] or "",
                },
            }

    errors = {"n": 0}
    fed = {"n": 0}

    def callback(response: VespaResponse, doc_id: str):
        if response.is_successful():
            fed["n"] += 1
            if fed["n"] % 250 == 0:
                print(f"   ...fed {fed['n']} docs")
        else:
            errors["n"] += 1
            if errors["n"] <= 5:
                print(f"   ! error feeding {doc_id}: {response.get_json()}")

    print(">> Feeding (Vespa embeds each doc as it arrives — this is the work)...")
    t1 = time.time()
    app.feed_iterable(feed_docs(), schema=SCHEMA, namespace=NAMESPACE, callback=callback)
    dt = time.time() - t1

    # confirm how many are searchable
    total = None
    try:
        r = app.query(yql="select * from sources * where true", hits=0)
        total = r.json.get("root", {}).get("fields", {}).get("totalCount")
    except Exception as e:  # noqa: BLE001
        print(f"   (could not read totalCount: {e})")

    print("\n========================================")
    print(f"  Fed {fed['n']} documents in {dt:.0f}s ({errors['n']} errors).")
    if total is not None:
        print(f"  Vespa reports {total} searchable documents.")
    print("  Next:  python 02_search.py")
    print("========================================")


if __name__ == "__main__":
    main()
