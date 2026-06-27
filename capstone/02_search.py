"""
Step 2: The intuition-builder. Run the SAME queries three ways and compare:

    bm25     = pure keyword search   (misses synonyms / meaning)
    semantic = pure vector search    (misses exact terms / rare words)
    fusion   = hybrid, RRF-fused     (gets both -> usually best)

This is the demo to show live. Edit QUERIES below to your own and re-run — watching
where each method wins/loses is how the concepts click.

    Usage:  python 02_search.py
"""

from app_package import connect_local, search_ids

QUERIES = [
    "How do fruits and vegetables help with asthma?",
    "ketogenic diet effects on cancer",
    "does coffee increase the risk of heart disease",
    "vitamin D and bone health",
]

MODES = ["bm25", "semantic", "fusion"]
TOP = 3


def main():
    app = connect_local()
    with app.syncio(connections=1) as session:
        for q in QUERIES:
            print("\n" + "=" * 88)
            print(f"QUERY:  {q}")
            print("=" * 88)
            for mode in MODES:
                print(f"\n  [{mode.upper()}]")
                try:
                    results = search_ids(session, q, mode, hits=TOP)
                except Exception as e:  # noqa: BLE001
                    print(f"    ! query failed: {e}")
                    continue
                if not results:
                    print("    (no hits)")
                for rank, (doc_id, title, rel) in enumerate(results, 1):
                    title = (title or "").strip()[:80]
                    print(f"    {rank}. ({rel:.4f})  {title or '<no title>'}   [{doc_id}]")

    print("\nTip: notice how the top hit changes per method. Hybrid usually surfaces the")
    print("     doc that's both topically close AND uses the right words. Next: 03_evaluate.py")


if __name__ == "__main__":
    main()
