"""
Central config + the LLM cost-safety policy.

Design principle (this is the interview point): an LLM in front of consumer search
traffic is a business risk — per-query cost x QPS x 125k caterers, plus latency and
prompt-injection from untrusted input. So:

  * LLM_INDEX  (default AUTO = on iff a key exists): index-time LLM — PDF parsing and
                enrichment. Offline, once per item, CACHED. Bounded, amortized cost.
                This is the ONLY place LLM spend is defensible.
  * LLM_QUERY  (default OFF, even if a key exists): query-time understanding runs on a
                deterministic heuristic parser by default. The LLM is an opt-in toggle
                you flip on to *demo* the capability — never the default hot path.

Everything runs end-to-end with NO key and NO external calls.
"""

from __future__ import annotations

import os
from pathlib import Path

_HERE = Path(__file__).resolve()
# repo root candidates: .../ezcater-demo/ingest -> ezcater-demo -> repo root
_ENV_CANDIDATES = [
    _HERE.parents[2] / ".env",   # repo root
    _HERE.parents[1] / ".env",   # ezcater-demo/.env
    Path.cwd() / ".env",
]


def _load_dotenv() -> None:
    """Tiny KEY=VALUE loader (no python-dotenv dep). Does not overwrite real env vars."""
    for envf in _ENV_CANDIDATES:
        try:
            if not envf.is_file():
                continue
            for line in envf.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                os.environ.setdefault(k, v)
        except Exception:  # noqa: BLE001
            pass


_load_dotenv()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()
OPENAI_VISION_MODEL = os.environ.get("OPENAI_VISION_MODEL", OPENAI_MODEL).strip()

REDIS_URL = os.environ.get("REDIS_URL", "").strip()  # empty -> disk-backed cache


def has_key() -> bool:
    return bool(OPENAI_API_KEY)


def index_llm_enabled() -> bool:
    """Index-time LLM (PDF parse + enrichment). AUTO = on iff a key exists."""
    mode = os.environ.get("LLM_INDEX", "auto").strip().lower()
    if mode == "off":
        return False
    if mode == "on":
        return has_key()  # can't run without a key regardless
    return has_key()      # auto


def query_llm_enabled() -> bool:
    """Query-time understanding. Default AUTO = on iff a key exists — affordable because a
    SEMANTIC CACHE (semcache.py) bounds cost to one LLM call per intent, not per query.
    Set LLM_QUERY=off to force the deterministic regex parser."""
    mode = os.environ.get("LLM_QUERY", "auto").strip().lower()
    if mode == "off":
        return False
    return has_key()  # auto / on


def status() -> dict:
    return {
        "has_key": has_key(),
        "index_llm": index_llm_enabled(),
        "query_llm": query_llm_enabled(),
        "model": OPENAI_MODEL,
        "vision_model": OPENAI_VISION_MODEL,
        "cache": "redis" if REDIS_URL else "disk",
    }
