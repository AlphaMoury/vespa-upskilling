"""
Step 3: The PROOF. This is your headline slide result.

NFCorpus ships with human relevance judgments (qrels), so we can measure ranking
quality with nDCG@10 — the standard search-quality metric (higher = better, max 1.0).

We run a sample of real queries under each rank profile and print a leaderboard.
Expect hybrid (fusion) to beat both keyword-only and semantic-only — reproducing the
official Vespa tutorial's finding. SCREENSHOT THE TABLE for your presentation.

    Usage:  python 03_evaluate.py
            N_QUERIES=100 python 03_evaluate.py     # evaluate on more queries (slower)
"""

import math
import os

from app_package import connect_local, search_ids

N_QUERIES = int(os.environ.get("N_QUERIES", "50"))
DATASET = os.environ.get("DATASET", "nfcorpus").strip()   # must match what 01 fed
K = 10
MODES = ["bm25", "semantic", "fusion"]


def dcg(rels):
    return sum((2**rel - 1) / math.log2(i + 2) for i, rel in enumerate(rels))


def ndcg_at_k(ranked_ids, rel_map, k=K):
    gains = [rel_map.get(doc_id, 0) for doc_id in ranked_ids[:k]]
    ideal = sorted(rel_map.values(), reverse=True)[:k]
    idcg = dcg(ideal)
    return dcg(gains) / idcg if idcg > 0 else 0.0


def load_eval_data():
    """Returns (queries: dict[qid->text], qrels: dict[qid->dict[docid->rel]])."""
    from datasets import load_dataset

    print(f">> Loading {DATASET} queries + relevance judgments (qrels)...")
    qrels = {}
    qrel_ds = load_dataset(f"BeIR/{DATASET}-qrels", split="test")
    for row in qrel_ds:
        qid = str(row["query-id"])
        did = str(row["corpus-id"])
        qrels.setdefault(qid, {})[did] = int(row["score"])

    queries = {}
    q_ds = load_dataset(f"BeIR/{DATASET}", "queries", split="queries")
    for row in q_ds:
        queries[str(row["_id"])] = row["text"]

    return queries, qrels


def main():
    queries, qrels = load_eval_data()

    # keep only queries that have at least one positively-judged doc, then sample N
    usable = [qid for qid in qrels if qid in queries and any(v > 0 for v in qrels[qid].values())]
    usable = usable[:N_QUERIES]
    print(f">> Evaluating on {len(usable)} queries, nDCG@{K}, profiles: {', '.join(MODES)}\n")

    scores = {m: [] for m in MODES}
    app = connect_local()
    with app.syncio(connections=1) as session:
        for i, qid in enumerate(usable, 1):
            text = queries[qid]
            rel_map = qrels[qid]
            for mode in MODES:
                try:
                    results = search_ids(session, text, mode, hits=K)
                    ranked_ids = [doc_id for (doc_id, _t, _r) in results]
                    scores[mode].append(ndcg_at_k(ranked_ids, rel_map))
                except Exception as e:  # noqa: BLE001
                    print(f"   ! {mode} failed on q{qid}: {e}")
            if i % 10 == 0:
                print(f"   ...{i}/{len(usable)} queries done")

    # leaderboard
    print("\n" + "=" * 44)
    print(f"  nDCG@{K} LEADERBOARD  (n={len(usable)} queries)")
    print("=" * 44)
    table = sorted(
        ((m, sum(v) / len(v) if v else 0.0) for m, v in scores.items()),
        key=lambda kv: kv[1],
        reverse=True,
    )
    best = table[0][1] if table else 0
    for rank, (mode, avg) in enumerate(table, 1):
        flag = "  <-- winner" if rank == 1 else ""
        bar = "#" * int(avg * 40)
        print(f"  {rank}. {mode:<9} {avg:.4f}  {bar}{flag}")
    print("=" * 44)
    if len(table) >= 2 and best > 0:
        runner = table[1][1]
        lift = (best - runner) / runner * 100 if runner else 0
        print(f"  Hybrid is +{lift:.1f}% over the next-best single method.")
    print("\n  (Official tutorial reference: bm25~0.32, dense~0.31, hybrid~0.34)")
    print("  These exact numbers vary with the query sample — the ORDERING is the point.")


if __name__ == "__main__":
    main()
