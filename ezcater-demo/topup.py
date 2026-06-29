"""
Top up the live covid + question indexes to the FULL corpora, feeding only the docs
not already present (skips the first N already fed). Runs against the live container.

  python topup.py                  # quora 150k->full, covid 50k->full
  QUORA_SKIP=150000 COVID_SKIP=50000 python topup.py

Requires Docker memory >= ~12GB (so the full ~695k docs fit).
"""

import os
import time
import requests
from vespa.application import Vespa
from datasets import load_dataset

QUORA_SKIP = int(os.environ.get("QUORA_SKIP", "150000"))
COVID_SKIP = int(os.environ.get("COVID_SKIP", "50000"))
app = Vespa(url="http://localhost", port=8080)
S = "http://localhost:8080/search/"


def count(schema):
    return requests.get(S, params={"yql": f"select * from {schema} where true", "hits": 0}, timeout=10).json()["root"]["fields"]["totalCount"]


def topup(dsname, cfg, mapper, schema, skip, label):
    ds = load_dataset(dsname, cfg, split=cfg, streaming=True)

    def gen():
        n = 0
        for x in ds:
            n += 1
            if n <= skip:
                continue
            yield mapper(x)

    st = {"n": 0, "err": 0, "tl": time.time(), "nl": 0}

    def cb(r, i):
        if r.is_successful():
            st["n"] += 1
            if st["n"] % 5000 == 0:
                now = time.time()
                rate = (st["n"] - st["nl"]) / max(now - st["tl"], 1e-6)
                print(f"   {label}: +{st['n']:,} ({rate:,.0f}/s) -> {count(schema):,} total", flush=True)
                st["tl"], st["nl"] = now, st["n"]
        else:
            st["err"] += 1

    app.feed_iterable(gen(), schema=schema, namespace="ezcater", callback=cb,
                      max_queue_size=4000, max_workers=12, max_connections=12)
    print(f"   {label}: DONE +{st['n']:,} ({st['err']} err) -> {count(schema):,} total", flush=True)


print(f">> QUORA top-up (skip {QUORA_SKIP:,})...", flush=True)
topup("BeIR/quora", "corpus", lambda x: {"id": x["_id"], "fields": {"id": x["_id"], "text": x.get("text") or ""}},
      "question", QUORA_SKIP, "quora")
print(f">> COVID top-up (skip {COVID_SKIP:,})...", flush=True)
topup("BeIR/trec-covid", "corpus", lambda x: {"id": x["_id"], "fields": {"id": x["_id"], "title": x.get("title") or "", "body": x.get("text") or ""}},
      "covid", COVID_SKIP, "covid")
print(">> ALL DONE", flush=True)
