"""
Unified ingestion entrypoint — the same pipeline for every source.

    source --(adapter)--> MenuItem --(enrich)--> Vespa `dish` doc

Examples (run from ezcater-demo/, via ../capstone/.venv/bin/python):
    # inspect only (no Vespa needed) — enrich 5 real recipes and print them
    python -m ingest.run_ingest --source hf --limit 5 --print

    # grow + persist the ONTOLOGY GRAPH only — no Vespa, no index, nothing to feed
    python -m ingest.run_ingest --source hf --limit 3000 --graph-only

    # one-time: redeploy schema (adds `source` field) then feed 3000 real recipes
    python -m ingest.run_ingest --source hf --limit 3000 --deploy

    # ingest menu PDFs from ./menus (vision-LLM if OPENAI key, else text parse)
    python -m ingest.run_ingest --source pdf --deploy

    # replay the synthetic catalog through the unified pipeline
    python -m ingest.run_ingest --source synthetic

Flags: --dataset foodcom|datahive (hf), --path PATH (pdf/synthetic), --deploy (redeploy
first), --print (dry-run, no feed), --graph-only (ontology only, no Vespa), --limit N.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# allow `python -m ingest.run_ingest` from ezcater-demo/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest.adapters import REGISTRY          # noqa: E402
from ingest.menu_item import iter_items        # noqa: E402
from ingest.enrich import enrich               # noqa: E402
from ingest import cache, config               # noqa: E402
from ingest.graph import get_graph             # noqa: E402

NAMESPACE = "ezcater"
SCHEMA = "dish"


def build_adapter(args):
    cls = REGISTRY[args.source]
    if args.source == "hf":
        return cls(dataset=args.dataset)
    if args.source in ("pdf", "synthetic", "table"):
        return cls(path=args.path) if args.path else cls()
    return cls()


def enriched_docs(adapter, limit, stats):
    """Yield Vespa docs; enrich each MenuItem and track quality stats."""
    for item in iter_items(adapter, limit=limit):
        enrich(item)
        stats["n"] += 1
        if item.confidence < 0.7:
            stats["low_conf"] += 1
        if item.allergens:
            stats["with_allergens"] += 1
        yield item.to_vespa_doc()


def main():
    ap = argparse.ArgumentParser(description="Multi-source ingestion -> Vespa dish index")
    ap.add_argument("--source", required=True, choices=sorted(REGISTRY))
    ap.add_argument("--dataset", default="foodcom", help="hf profile: foodcom|datahive")
    ap.add_argument("--path", default=None, help="pdf dir/file or dishes.jsonl")
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--deploy", action="store_true", help="redeploy schema first (additive)")
    ap.add_argument("--print", dest="dry", action="store_true", help="dry-run: enrich + print, no feed")
    ap.add_argument("--graph-only", action="store_true",
                    help="grow + save the ontology graph only: no Vespa, no feed")
    args = ap.parse_args()

    print(f">> ingest source={args.source} limit={args.limit}  LLM={config.status()}")
    adapter = build_adapter(args)
    stats = {"n": 0, "low_conf": 0, "with_allergens": 0}

    # ---- no-Vespa paths. enrich() is what GROWS the graph, so both persist it: --print
    #      previews a handful of docs, --graph-only runs the full --limit for the ontology.
    if args.dry or args.graph_only:
        n = 0
        for item in iter_items(adapter, limit=min(args.limit, 20) if args.dry else args.limit):
            enrich(item)
            n += 1
            if args.dry:
                print(json.dumps(item.as_dict(), ensure_ascii=False)[:1000])
            elif n % 200 == 0:
                print(f"   enriched {n:,}  (cache hits={cache.stats['hits']})")
        g = get_graph()
        g.save()
        print(f"\n[no-vespa] enriched={n:,}  cache={cache.backend()} "
              f"hits={cache.stats['hits']} misses={cache.stats['misses']}")
        print(f"   ontology graph: {g.stats()}  -> saved")
        return

    # ---- feed to Vespa ----
    from vespa.application import Vespa
    from vespa.deployment import VespaDocker

    if args.deploy:
        from app_package import package
        print(">> deploying (additive) ...")
        t0 = time.time()
        app = VespaDocker(port=8080).deploy(application_package=package)
        print(f">> deployed in {time.time()-t0:.0f}s")
    else:
        app = Vespa(url="http://localhost", port=8080)

    fed = {"ok": 0, "err": 0}

    def cb(resp, doc_id):
        if resp.is_successful():
            fed["ok"] += 1
            if fed["ok"] % 1000 == 0:
                print(f"   fed {fed['ok']:,}  (cache hits={cache.stats['hits']})")
        else:
            fed["err"] += 1
            if fed["err"] <= 3:
                print(f"   ! {doc_id}: {resp.get_json()}")

    t0 = time.time()
    app.feed_iterable(enriched_docs(adapter, args.limit, stats), schema=SCHEMA,
                      namespace=NAMESPACE, callback=cb,
                      max_queue_size=2000, max_workers=8, max_connections=8)
    dt = time.time() - t0
    print(f"\n>> done: fed {fed['ok']:,} ({fed['err']} err) in {dt:.0f}s")
    print(f"   enriched={stats['n']:,}  with_allergens={stats['with_allergens']:,}  "
          f"low_confidence(<0.7)={stats['low_conf']:,}")
    print(f"   enrichment cache: backend={cache.backend()} hits={cache.stats['hits']} misses={cache.stats['misses']}")
    g = get_graph()
    g.save()
    print(f"   ontology graph: {g.stats()}  -> saved")


if __name__ == "__main__":
    main()
