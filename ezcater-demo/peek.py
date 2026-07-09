"""
Peek at what's actually stored in Vespa — no Vespa CLI needed, just HTTP on :8080.

    ../capstone/.venv/bin/python peek.py                    # counts + a few sample dish docs
    ../capstone/.venv/bin/python peek.py brisket            # dish docs whose text matches "brisket"
    ../capstone/.venv/bin/python peek.py --schema covid vaccine
    ../capstone/.venv/bin/python peek.py --id foodcom-2602.0

Two Vespa HTTP APIs are used (both work in a browser too):
  * Document API  /document/v1/<ns>/<schema>/docid[/<id>]   -> the raw STORED document
  * Search API    /search/?yql=...                          -> a YQL query (ranked)
"""
from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request

VESPA = "http://localhost:8080"
NS = "ezcater"


def _get(path: str) -> dict:
    with urllib.request.urlopen(VESPA + path, timeout=20) as r:
        return json.load(r)


def _count(schema: str) -> int:
    yql = urllib.parse.quote(f"select * from {schema} where true")
    return _get(f"/search/?yql={yql}&hits=0")["root"]["fields"]["totalCount"]


def _show(fields: dict) -> None:
    print(json.dumps(fields, indent=2, ensure_ascii=False))
    print("-" * 60)


def main() -> None:
    ap = argparse.ArgumentParser(description="Inspect Vespa documents over HTTP")
    ap.add_argument("query", nargs="*", help="text to match (dish name/description by default)")
    ap.add_argument("--schema", default="dish", help="dish | covid | question")
    ap.add_argument("--id", help="fetch one document by its id")
    ap.add_argument("-n", type=int, default=3, help="how many docs to show")
    args = ap.parse_args()
    schema = args.schema

    print(f"# counts:  dish={_count('dish'):,}  covid={_count('covid'):,}  question={_count('question'):,}\n")

    if args.id:
        doc = _get(f"/document/v1/{NS}/{schema}/docid/{urllib.parse.quote(args.id)}")
        _show(doc.get("fields", {}))
        return

    if args.query:
        q = " ".join(args.query)
        print(f"# search {schema!r} for {q!r}:\n")
        yql = urllib.parse.quote(f"select * from {schema} where userQuery()")
        d = _get(f"/search/?yql={yql}&query={urllib.parse.quote(q)}&hits={args.n}&ranking=bm25")
        hits = d.get("root", {}).get("children", []) or []
        print(f"# {d['root']['fields'].get('totalCount', 0):,} total matches; showing {len(hits)}\n")
        for h in hits:
            _show(h.get("fields", {}))
        return

    # default: a few raw stored docs straight from the document store
    print(f"# {args.n} sample {schema!r} documents (raw stored fields):\n")
    d = _get(f"/document/v1/{NS}/{schema}/docid?wantedDocumentCount={args.n}")
    for doc in d.get("documents", []):
        _show(doc.get("fields", {}))


if __name__ == "__main__":
    main()
