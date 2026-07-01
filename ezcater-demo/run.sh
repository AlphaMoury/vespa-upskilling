#!/usr/bin/env bash
# Launch the EzCater x Vespa demo.
#   bash run.sh          -> if Vespa already has data, JUST start API + UI (fast, no re-feed)
#   FRESH=1 bash run.sh  -> rebuild everything (deploy + re-feed all indexes; slow, ~40 min)
# Requires Docker running + the capstone venv (../capstone/setup.sh).
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="$ROOT/../capstone/.venv/bin/python"

# current number of dish docs in Vespa (empty/0 if Vespa is down or empty)
dish_count() {
  curl -s -G "http://localhost:8080/search/" \
    --data-urlencode "yql=select * from dish where true" \
    --data-urlencode "hits=0" \
    --data-urlencode "timeout=3s" 2>/dev/null \
    | grep -o '"totalCount":[0-9]*' | head -1 | grep -o '[0-9]*' || true
}

N="$(dish_count)"
: "${N:=0}"

if [ "${FRESH:-0}" = "1" ] || [ "$N" -eq 0 ]; then
  [ -f "$ROOT/data/dishes.jsonl" ] || python3 "$ROOT/data/build_dataset.py"
  echo ">> deploy + feed Vespa (FRESH build — this is the slow part)..."
  ( cd "$ROOT" && "$PY" deploy_and_feed.py )
else
  echo ">> Vespa already has data ($N dishes) — skipping deploy+feed. (FRESH=1 to rebuild.)"
fi

echo ">> API   -> http://localhost:8009"
( cd "$ROOT/server" && "$PY" -m uvicorn main:app --port 8009 ) &
API_PID=$!
trap 'kill $API_PID 2>/dev/null || true' EXIT

echo ">> UI    -> http://localhost:5173   (open this)"
cd "$ROOT/web"
[ -d node_modules ] || npm install
npm run dev
