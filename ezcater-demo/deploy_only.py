"""
Redeploy the Vespa app package (schema + RANK PROFILES) to the already-running
container WITHOUT feeding any documents.

Why this exists / why it is safe:
  In Vespa, RANKING is decoupled from INDEXING. A rank-profile change (first/second-phase
  expressions, RRF, an imported LTR/ONNX model, or blending in marketplace signals that are
  ALREADY indexed as attributes — popularity, serves, price_pp) applies as a live config
  update: the content nodes keep their document stores, so there is NO reindex and NO
  re-feed. Only changing how a field is STORED or EMBEDDED (e.g. adding the e5
  'query:'/'passage:' prefixes, or adding a brand-new indexed field) requires
  re-embedding / reindexing the corpus.

Usage (from ezcater-demo/, via the capstone venv):
    ../capstone/.venv/bin/python deploy_only.py

Prints the dish doc count before and after so you can confirm nothing was wiped.
"""

from vespa.application import Vespa
from vespa.deployment import VespaDocker

from app_package import package


def dish_count():
    try:
        r = Vespa(url="http://localhost", port=8080).query(
            yql="select * from dish where true", hits=0, timeout="5s")
        return r.json.get("root", {}).get("fields", {}).get("totalCount", "?")
    except Exception as e:  # noqa: BLE001
        return f"(unavailable: {e})"


if __name__ == "__main__":
    before = dish_count()
    print(f">> dish docs before deploy: {before}")
    print(">> redeploying app package (schema + rank profiles) — NO feed ...")
    VespaDocker(port=8080).deploy(application_package=package)
    after = dish_count()
    print(f">> dish docs after deploy:  {after}")
    print(">> done — new rank profiles are live; documents preserved (no reindex).")
