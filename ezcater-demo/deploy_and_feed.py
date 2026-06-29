"""
Deploy the EzCater two-schema app to local Docker and feed caterers + dishes.
Vespa generates the e5 embedding for each record on ingest.

    python deploy_and_feed.py

Run data/build_dataset.py first. Make sure port 8080 is free (stop any other Vespa).
Uses the capstone venv (pyvespa). Run from the ezcater-demo/ directory.
"""

import json
import time
from pathlib import Path

from vespa.deployment import VespaDocker
from vespa.io import VespaResponse

from app_package import package, NAMESPACE

HERE = Path(__file__).resolve().parent


def load(path):
    return [json.loads(line) for line in (HERE / "data" / path).read_text().splitlines() if line.strip()]


def feed(app, docs, schema):
    errors = {"n": 0}

    def cb(resp: VespaResponse, doc_id: str):
        if not resp.is_successful():
            errors["n"] += 1
            if errors["n"] <= 5:
                print(f"   ! {schema} {doc_id}: {resp.get_json()}")

    t = time.time()
    app.feed_iterable(docs, schema=schema, namespace=NAMESPACE, callback=cb)
    print(f"   fed {len(docs)} {schema}(s) in {time.time()-t:.0f}s ({errors['n']} errors)")


def main():
    print(">> Deploying EzCater app (2 schemas: caterer + dish)...")
    t0 = time.time()
    app = VespaDocker(port=8080).deploy(application_package=package)
    print(f">> Ready in {time.time()-t0:.0f}s @ http://localhost:8080")

    print(">> Feeding caterers...")
    feed(app, load("caterers.jsonl"), "caterer")
    print(">> Feeding dishes...")
    feed(app, load("dishes.jsonl"), "dish")

    for sch in ("caterer", "dish"):
        try:
            r = app.query(yql=f"select * from {sch} where true", hits=0)
            print(f"   {sch}: {r.json.get('root',{}).get('fields',{}).get('totalCount')} searchable")
        except Exception as e:  # noqa: BLE001
            print(f"   ({sch} count failed: {e})")
    print("\nDone. Start the API:  cd server && ../../capstone/.venv/bin/python -m uvicorn main:app --port 8000")


if __name__ == "__main__":
    main()
