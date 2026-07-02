"""
Semantic cache for query understanding — what makes LLM-first affordable at scale.

The objection to "LLM on every query" is cost/latency at high QPS. The fix: cache the
structured result by the query's *embedding*, so paraphrases of the same intent reuse a
prior answer. You pay the understanding LLM once per INTENT, not once per keystroke.

  "vegan lunch no nuts"  ~  "plant-based midday, nut free"  -> same cached concepts

Uses a small OpenAI embedding (text-embedding-3-small, 256-dim) as the key; cosine match
above a threshold is a hit. Degrades to an exact-text cache if there's no key. Persisted
to disk so the cache survives restarts.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import re
from typing import Optional

_PATH = Path(__file__).resolve().parent.parent / "data" / ".semcache.json"

# A cosine-only cache can false-hit: "spread WITH MEAT, no nuts" vs "spread, no nuts" are ~0.98
# similar in text, so the embedding treats them as the same intent — but they aren't. Guard the
# semantic tier by requiring the two queries share the same set of INTENT-FLIPPING content words
# (diet / protein / allergen), after light plural + synonym normalization. Spice/price/occasion
# differences are ignored (minor). Erring toward a miss is safe (it just re-calls the LLM).
_GUARD_VOCAB = {"nut", "gluten", "dairy", "cheese", "egg", "soy", "shellfish", "fish", "sesame",
                "meat", "vegan", "vegetarian", "pescatarian", "halal", "kosher"}
_GUARD_SYN = {"nuts": "nut", "peanut": "nut", "peanuts": "nut", "eggs": "egg", "cheeses": "cheese",
              "plant": "vegan", "plantbased": "vegan", "veggie": "vegetarian",
              "beef": "meat", "chicken": "meat", "pork": "meat", "lamb": "meat", "turkey": "meat",
              "poultry": "meat", "bacon": "meat", "sausage": "meat", "carne": "meat", "brisket": "meat",
              "seafood": "fish", "shrimp": "shellfish"}


def _guard_tokens(text: str) -> frozenset:
    s = set()
    for w in re.findall(r"[a-z]+", (text or "").lower()):
        w = _GUARD_SYN.get(w, w)
        if w.endswith("s") and w[:-1] in _GUARD_VOCAB:
            w = w[:-1]
        if w in _GUARD_VOCAB:
            s.add(w)
    return frozenset(s)
# Cache keys are embedded with the SAME e5-small-v2 family used for search — run LOCALLY
# (sentence-transformers), so a semantic hit is ~10-30 ms with no external call. e5 is 384-d.
MODEL = "e5-small-v2 (local)"
_DIM = 384
# calibrated on e5-small-v2: paraphrases ~0.86-0.90, distinct-but-food-related ~0.81-0.82.
# 0.87 keeps a safe margin above distinct (false hits return wrong concepts — worse than a
# miss, which just re-calls the LLM), while still catching clear paraphrases.
_THRESH = 0.87
_MAX = 500

stats = {"hits": 0, "misses": 0}
_store: list[dict] = []   # [{vec:[...], text:str, value:{...}}]
_exact: dict[str, dict] = {}


def _load():
    global _store, _exact
    try:
        if _PATH.is_file():
            d = json.loads(_PATH.read_text())
            _store = d.get("store", [])
            _exact = d.get("exact", {})
    except Exception:  # noqa: BLE001
        _store, _exact = [], {}


def _save():
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        _PATH.write_text(json.dumps({"store": _store[-_MAX:], "exact": _exact}))
    except Exception:  # noqa: BLE001
        pass


_load()


_model = None
_model_lock = __import__("threading").Lock()


def _e5():
    """Lazy-load the local e5-small-v2 (same family Vespa uses for search). No external call."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer
                _model = SentenceTransformer("intfloat/e5-small-v2")
    return _model


def warmup() -> None:
    """Preload the model so the first cache query isn't slowed by model load."""
    try:
        _e5()
    except Exception:  # noqa: BLE001
        pass


def _embed(text: str) -> Optional[list[float]]:
    try:
        # e5 expects a "query:" prefix; normalize so cosine == dot product
        v = _e5().encode("query: " + text, normalize_embeddings=True)
        return [float(x) for x in v]
    except Exception:  # noqa: BLE001
        return None


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def get(text: str) -> Optional[dict]:
    """Two-tier cache. Tier 1 (exact text) is instant — no network. Tier 2 (semantic)
    embeds the query to match paraphrases. Returns cached concepts, or None on a miss."""
    key = text.strip().lower()
    # tier 1: exact text — the same query returns instantly, no embedding call
    if key in _exact:
        stats["hits"] += 1
        return {**_exact[key], "_cache": "exact"}
    # tier 2: semantic — embed and match by cosine (this is the only tier that hits the API)
    v = _embed(text)
    if v is None:
        stats["misses"] += 1
        return None
    best, best_sim = None, 0.0
    for e in _store:
        s = _cos(v, e["vec"])
        if s > best_sim:
            best, best_sim = e, s
    if best and best_sim >= _THRESH and _guard_tokens(text) == _guard_tokens(best["text"]):
        stats["hits"] += 1
        return {**best["value"], "_sim": round(best_sim, 3), "_matched": best["text"], "_cache": "semantic"}
    stats["misses"] += 1
    return None


def put(text: str, value: dict) -> None:
    key = text.strip().lower()
    _exact[key] = value
    v = _embed(text)
    if v is not None:
        _store.append({"vec": v, "text": text, "value": value})
        del _store[:-_MAX]
    _save()


def size() -> int:
    return len(_store) or len(_exact)
