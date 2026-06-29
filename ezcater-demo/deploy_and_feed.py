"""
Deploy the 3-schema app and feed all three indexes:
  dish     <- data/dishes.jsonl              (EzCater catering, ~600)
  covid    <- BeIR/trec-covid corpus         (COVID_N docs, default 50k)
  question <- BeIR/quora corpus              (QUORA_N docs, default 150k)

  python deploy_and_feed.py
  COVID_N=80000 QUORA_N=200000 python deploy_and_feed.py

Vespa generates the e5 embedding per doc on ingest. Make port 8080 free first.
"""

import json
import os
import time
from pathlib import Path

from vespa.deployment import VespaDocker
from vespa.io import VespaResponse

from app_package import package, NAMESPACE

HERE = Path(__file__).resolve().parent
COVID_N = int(os.environ.get("COVID_N", "50000"))
QUORA_N = int(os.environ.get("QUORA_N", "150000"))


def feed(app, docs, schema, total=None):
    state = {"n": 0, "err": 0, "t": time.time(), "tl": time.time(), "nl": 0}

    def cb(resp: VespaResponse, doc_id: str):
        if resp.is_successful():
            state["n"] += 1
            if state["n"] % 5000 == 0:
                now = time.time()
                rate = (state["n"] - state["nl"]) / max(now - state["tl"], 1e-6)
                print(f"     {schema}: {state['n']:,}{'/' + format(total, ',') if total else ''}  ({rate:,.0f}/s)")
                state["tl"], state["nl"] = now, state["n"]
        else:
            state["err"] += 1
            if state["err"] <= 3:
                print(f"     ! {schema} {doc_id}: {resp.get_json()}")

    app.feed_iterable(docs, schema=schema, namespace=NAMESPACE, callback=cb,
                      max_queue_size=4000, max_workers=12, max_connections=12)
    print(f"   {schema}: fed {state['n']:,} in {time.time()-state['t']:.0f}s ({state['err']} err)")


def jsonl(path):
    docs = []
    for x in (HERE / "data" / path).read_text().splitlines():
        if not x.strip():
            continue
        d = json.loads(x)
        d["fields"].pop("caterer_id", None)  # not in the dish schema
        docs.append(d)
    return docs


def stream(name, cfg, mapper, limit):
    from datasets import load_dataset
    ds = load_dataset(name, cfg, split=cfg, streaming=True)
    n = 0
    for x in ds:
        if n >= limit:
            break
        n += 1
        yield mapper(x)


def main():
    print(">> Deploying 3-schema app (dish + covid + question)...")
    t0 = time.time()
    app = VespaDocker(port=8080).deploy(application_package=package)
    print(f">> Ready in {time.time()-t0:.0f}s")

    print(">> [1/3] catering dishes...")
    feed(app, jsonl("dishes.jsonl"), "dish")

    print(f">> [2/3] trec-covid papers (N={COVID_N:,})...")
    feed(app, stream("BeIR/trec-covid", "corpus",
                     lambda x: {"id": x["_id"], "fields": {"id": x["_id"], "title": x.get("title") or "", "body": x.get("text") or ""}},
                     COVID_N), "covid", COVID_N)

    print(f">> [3/3] quora questions (N={QUORA_N:,})...")
    feed(app, stream("BeIR/quora", "corpus",
                     lambda x: {"id": x["_id"], "fields": {"id": x["_id"], "text": x.get("text") or ""}},
                     QUORA_N), "question", QUORA_N)

    print("\n>> Final counts:")
    for sch in ("dish", "covid", "question"):
        try:
            r = app.query(yql=f"select * from {sch} where true", hits=0)
            print(f"   {sch:<10} {r.json.get('root',{}).get('fields',{}).get('totalCount'):>8,}")
        except Exception as e:  # noqa: BLE001
            print(f"   {sch}: count failed ({e})")
    print("\nDone. (Re)start API + UI:  bash run.sh   (or restart uvicorn :8009 and npm run dev)")


if __name__ == "__main__":
    main()
