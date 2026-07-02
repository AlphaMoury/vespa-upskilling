"""
Open Food Facts corroboration — the "authoritative ground truth" layer.

The curated taxonomy (taxonomy.py) is primary. This adds a live, keyless lookup against
Open Food Facts to corroborate/extend allergen labels for an ingredient. It is:
  * best-effort (network failures fall back silently to the taxonomy),
  * cached by ingredient term (so re-runs don't re-hit the API),
  * off by default in the disk-cache path unless OFF_LIVE=1 (keeps the demo offline-safe).

OFF returns `allergens_tags` like ["en:gluten", "en:milk"]; we map those to our vocab.
"""

from __future__ import annotations

import os
from typing import Optional

import requests

from . import cache

_UA = {"User-Agent": "ezcater-vespa-demo/0.1 (interview prep; contact dextronomo@gmail.com)"}
_SEARCH = "https://world.openfoodfacts.org/api/v2/search"

# OFF allergen tag -> our vocabulary
_OFF_MAP = {
    "gluten": "gluten", "milk": "dairy", "eggs": "eggs", "egg": "eggs",
    "nuts": "nuts", "tree-nuts": "nuts", "peanuts": "peanuts",
    "soybeans": "soy", "soy": "soy", "crustaceans": "shellfish", "molluscs": "shellfish",
    "fish": "fish", "sesame-seeds": "sesame", "sesame": "sesame",
}


def live_enabled() -> bool:
    return os.environ.get("OFF_LIVE", "0").strip() == "1"


def allergens_for_ingredient(term: str) -> set[str]:
    """Query OFF for a food term; return corroborated allergens (cached). Never raises."""
    term = (term or "").strip().lower()
    if not term or not live_enabled():
        return set()
    ckey = f"off:{term}"
    hit = cache.get_json(ckey)
    if hit is not None:
        return set(hit.get("allergens", []))
    found: set[str] = set()
    try:
        r = requests.get(_SEARCH, headers=_UA, timeout=6, params={
            "categories_tags_en": term,
            "fields": "allergens_tags",
            "page_size": 20,
        })
        if r.ok:
            for p in r.json().get("products", []):
                for tag in p.get("allergens_tags", []) or []:
                    key = tag.split(":")[-1]
                    if key in _OFF_MAP:
                        found.add(_OFF_MAP[key])
    except Exception:  # noqa: BLE001
        found = set()
    cache.set_json(ckey, {"allergens": sorted(found)})
    return found
