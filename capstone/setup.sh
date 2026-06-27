#!/usr/bin/env bash
# One-shot environment setup for the capstone.
#
# Your system Python is 3.14, which pyvespa does not support yet (it needs 3.10–3.13).
# `uv` solves this cleanly: it downloads Python 3.13 just for this project — no changes
# to your system Python, no pyenv, no Homebrew.
#
#   Usage:   bash setup.sh
#   Then:    source .venv/bin/activate
#            python 01_deploy_and_feed.py
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: 'uv' not found. Install it from https://docs.astral.sh/uv/ and re-run." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "WARNING: 'docker' not found on PATH. You need Docker running to deploy Vespa." >&2
fi

echo ">> Creating a Python 3.13 virtual environment with uv (.venv/)..."
uv venv --python 3.13 .venv

echo ">> Installing dependencies into .venv/..."
uv pip install --python .venv/bin/python -r requirements.txt

cat <<'EOF'

================================================================
 Setup complete.

 Next:
   source .venv/bin/activate
   python 01_deploy_and_feed.py     # deploy Vespa + feed data (slow first run)
   python 02_search.py              # compare keyword / semantic / hybrid
   python 03_evaluate.py            # nDCG@10 leaderboard (the proof)

 Make sure Docker is running with ~6–8 GB RAM allocated.
================================================================
EOF
