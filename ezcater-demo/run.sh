#!/usr/bin/env bash
# Launch the full EzCater x Vespa demo: deploy+feed Vespa, start the API, start the UI.
# Requires Docker running and the capstone venv (../capstone/setup.sh).
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="$ROOT/../capstone/.venv/bin/python"

if [ ! -f "$ROOT/data/dishes.jsonl" ]; then
  echo ">> generating dataset..."; python3 "$ROOT/data/build_dataset.py"
fi

echo ">> 1/3 deploy + feed Vespa (caterers + dishes)..."
( cd "$ROOT" && "$PY" deploy_and_feed.py )

echo ">> 2/3 starting API on http://localhost:8009 ..."
( cd "$ROOT/server" && "$PY" -m uvicorn main:app --port 8009 ) &
API_PID=$!
trap 'kill $API_PID 2>/dev/null || true' EXIT

echo ">> 3/3 starting React UI on http://localhost:5173 ..."
cd "$ROOT/web"
[ -d node_modules ] || npm install
npm run dev
