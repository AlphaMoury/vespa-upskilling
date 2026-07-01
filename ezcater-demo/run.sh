#!/usr/bin/env bash
# Launch the EzCater x Vespa demo.
#   bash run.sh          -> if Vespa already has data, JUST start API + UI (fast, no re-feed)
#   FRESH=1 bash run.sh  -> rebuild everything (deploy + re-feed all indexes; slow, ~40 min)
# Requires Docker running + the capstone venv (../capstone/setup.sh).
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="$ROOT/../capstone/.venv/bin/python"

have_data() {
  "$PY" - <<'PY' 2>/dev/null
import requests, sys
try:
    n = requests.get("http://localhost:8080/search/",
                     params={"yql": "select * from dish where true", "hits": 0, "timeout": "3s"},
                     timeout=5).json()["root"]["fields"]["totalCount"]
    sys.exit(0 if n and n > 0 else 1)
except Exception:
    sys.exit(1)
}

if [ "${FRESH:-0}" = "1" ] || ! have_data; then
  [ -f "$ROOT/data/dishes.jsonl" ] || python3 "$ROOT/data/build_dataset.py"
  echo ">> deploy + feed Vespa (FRESH build — this is the slow part)..."
  ( cd "$ROOT" && "$PY" deploy_and_feed.py )
else
  echo ">> Vespa already has data (skipping deploy+feed). Use FRESH=1 to rebuild."
fi

echo ">> API   -> http://localhost:8009"
( cd "$ROOT/server" && "$PY" -m uvicorn main:app --port 8009 ) &
API_PID=$!
trap 'kill $API_PID 2>/dev/null || true' EXIT

echo ">> UI    -> http://localhost:5173   (open this)"
cd "$ROOT/web"
[ -d node_modules ] || npm install
npm run dev
