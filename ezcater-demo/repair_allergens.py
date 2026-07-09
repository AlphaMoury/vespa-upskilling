"""
Re-derive the denormalized allergens[] for docs that carry an LLM false positive.

The ontology graph is the source of truth for allergens; each dish's `allergens` array is a
DENORMALIZED copy written at index time. When we correct the graph (taxonomy's curated
ALLERGEN_FALSE_POSITIVES veto — 'shells' is pasta, 'nutmeg' is a spice), the already-indexed
Vespa documents still carry the old labels.

Reindexing the corpus for that would be wasteful. `allergens` is an ATTRIBUTE, so instead we
find the affected docs and issue a targeted PARTIAL UPDATE — fast, in-place, no reindex.

Safety rule: an allergen is dropped only if (a) a denylisted ingredient in that doc claimed it
AND (b) no other ingredient in the doc justifies it under the corrected graph. We never remove
an allergen the graph still supports.

    ../capstone/.venv/bin/python repair_allergens.py --dry     # report only
    ../capstone/.venv/bin/python repair_allergens.py           # apply the updates
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests  # noqa: E402

from ingest import taxonomy  # noqa: E402
from ingest.graph import get_graph  # noqa: E402

VESPA = "http://localhost:8080"
NS = "ezcater"


def _search(yql: str, hits: int = 400) -> list[dict]:
    url = f"{VESPA}/search/?" + urllib.parse.urlencode({"yql": yql, "hits": hits, "timeout": "10s"})
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r).get("root", {}).get("children", []) or []


def main() -> None:
    ap = argparse.ArgumentParser(description="Repair false allergen labels in the Vespa dish index")
    ap.add_argument("--dry", action="store_true", help="report what would change, write nothing")
    args = ap.parse_args()

    g = get_graph()  # load() applies the false-positive veto to the graph
    candidates: dict[str, dict] = {}
    for ing, allergen in sorted(taxonomy.ALLERGEN_FALSE_POSITIVES):
        yql = f'select * from dish where ingredients contains "{ing}" and allergens contains "{allergen}"'
        for h in _search(yql):
            docid = (h.get("id") or "").split("::")[-1]
            if docid:
                candidates[docid] = h.get("fields", {})

    print(f"# {len(candidates)} candidate docs carry a denylisted (ingredient, allergen) pair\n")
    changed = 0
    for docid, f in sorted(candidates.items()):
        ings = f.get("ingredients") or []
        have = set(f.get("allergens") or [])
        good = set(g.enrich(ings)["allergens"])           # corrected graph's derivation
        suspect = {a for (i, a) in taxonomy.ALLERGEN_FALSE_POSITIVES if i in ings}
        new = have - (suspect - good)                     # drop only unjustified suspects
        if new == have:
            continue
        changed += 1
        print(f"  {f.get('name','?')[:52]:54s} {sorted(have)} -> {sorted(new)}")
        if not args.dry:
            r = requests.put(f"{VESPA}/document/v1/{NS}/dish/docid/{docid}",
                             json={"fields": {"allergens": {"assign": sorted(new)}}}, timeout=20)
            if not r.ok:
                print(f"    ! update failed for {docid}: {r.text[:120]}")

    verb = "would update" if args.dry else "updated"
    print(f"\n>> {verb} {changed} documents (partial update on the `allergens` attribute — no reindex)")


if __name__ == "__main__":
    main()
