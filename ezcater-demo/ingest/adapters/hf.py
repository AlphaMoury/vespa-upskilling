"""
HuggingFace recipe corpus -> MenuItem (source #1).

Proves the "general indexing pipeline" thesis at scale: a real, large corpus flowing
through the SAME schema and enrichment the synthetic data used. Streams a shard (no full
1M-row download). Recipes have no price/caterer, so we synthesize plausible catering
values deterministically (marked clearly) — that's the "reframe recipes as catering" step.

Two dataset profiles (pick with --dataset):
  foodcom  : AkashPS11/recipes_data_food.com        (MIT, ~1M rows, R-vector fields)
  datahive : datahiveai/recipes-with-nutrition      (CC BY-NC, has real diet/cuisine labels)
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Iterator

from ..menu_item import MenuItem

_CATERER_PREFIX = ["Bella", "Golden", "Urban", "Hearth", "Garden", "Maple", "Coastal",
                   "Sunrise", "Harvest", "Spice", "Olive", "Bamboo", "Saffron", "Verde"]
_CATERER_SUFFIX = ["Catering Co.", "Kitchen", "Caterers", "Catering", "Provisions", "Table", "Foods"]

_HEALTH_TO_DIET = {
    "vegan": "vegan", "vegetarian": "vegetarian", "gluten-free": "gluten-free",
    "dairy-free": "dairy-free", "pescatarian": "pescatarian", "paleo": "paleo", "keto-friendly": "keto",
}


def _h(s: str) -> int:
    return int(hashlib.sha1(s.encode()).hexdigest()[:8], 16)


def _synth_caterer(seed: str) -> str:
    h = _h(seed)
    return f"{_CATERER_PREFIX[h % len(_CATERER_PREFIX)]} {_CATERER_SUFFIX[(h // 7) % len(_CATERER_SUFFIX)]}"


def _synth_price(seed: str, serves: int) -> tuple[float, float]:
    pp = round(8.0 + (_h(seed) % 2200) / 100.0, 2)   # $8.00 - $30.00 per head (demo value)
    return round(pp * max(serves, 1), 2), pp


def _parse_r_vector(s: str) -> list[str]:
    """Parse R's c("a", "b") string (Food.com dumps ingredients this way)."""
    if not s or s in ("None", "character(0)"):
        return []
    return [m.strip() for m in re.findall(r'"([^"]*)"', s) if m.strip()]


def _parse_json_list(s) -> list:
    if isinstance(s, list):
        return s
    if not s:
        return []
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except Exception:  # noqa: BLE001
        return []


def _course_from(text: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in ["dessert", "cookie", "cake", "sweet", "pie", "frozen dessert"]):
        return "dessert"
    if any(k in t for k in ["breakfast", "brunch", "pancake", "waffle"]):
        return "breakfast"
    if any(k in t for k in ["appetizer", "salad", "soup", "side", "snack", "dip", "starter"]):
        return "appetizer"
    return "main"


def _int(v, default=0) -> int:
    try:
        return int(float(v))
    except Exception:  # noqa: BLE001
        return default


class HFRecipeAdapter:
    """SourceAdapter over a streamed HF recipe dataset."""
    name = "hf"

    PROFILES = {
        "foodcom": "AkashPS11/recipes_data_food.com",
        "datahive": "datahiveai/recipes-with-nutrition",
    }

    def __init__(self, dataset: str = "foodcom", split: str = "train"):
        self.profile = dataset if dataset in self.PROFILES else "foodcom"
        self.dataset_id = self.PROFILES[self.profile]
        self.split = split
        self.name = f"hf:{self.profile}"

    def fetch(self, **opts) -> Iterator[dict]:
        from datasets import load_dataset
        ds = load_dataset(self.dataset_id, split=self.split, streaming=True)
        for row in ds:
            yield row

    def to_menu_items(self, rec: dict) -> list[MenuItem]:
        return (self._foodcom(rec) if self.profile == "foodcom" else self._datahive(rec))

    # ---- profile: Food.com (MIT) ----
    def _foodcom(self, r: dict) -> list[MenuItem]:
        name = (r.get("Name") or "").strip()
        if not name:
            return []
        rid = str(r.get("RecipeId") or _h(name))
        desc = (r.get("Description") or "").strip()[:400]
        ingredients = _parse_r_vector(r.get("RecipeIngredientParts") or "")
        category = (r.get("RecipeCategory") or "").strip()
        keywords = " ".join(_parse_r_vector(r.get("Keywords") or ""))
        serves = _int(r.get("RecipeServings"), 0) or 10
        price, pp = _synth_price(rid, serves)
        nutrition = {k: r.get(k) for k in ("Calories", "ProteinContent", "CarbohydrateContent", "FatContent") if r.get(k)}
        return [MenuItem(
            id=f"foodcom-{rid}", name=name, description=desc,
            cuisine=category or None, course=_course_from(category + " " + keywords),
            serves=serves, price=price, price_pp=pp,
            caterer_name=_synth_caterer(rid), popularity=_int(r.get("ReviewCount"), 0),
            source="hf:foodcom", ingredients_raw=ingredients,
        )]

    # ---- profile: DataHive (CC BY-NC) — richer declared labels ----
    def _datahive(self, r: dict) -> list[MenuItem]:
        name = (r.get("recipe_name") or "").strip()
        if not name:
            return []
        rid = str(_h(name + str(r.get("url") or "")))
        health = [str(x).lower() for x in _parse_json_list(r.get("health_labels"))]
        declared = [_HEALTH_TO_DIET[h] for h in health if h in _HEALTH_TO_DIET]
        cuisines = _parse_json_list(r.get("cuisine_type"))
        cuisine = (cuisines[0].title() if cuisines else None)
        dish_type = " ".join(_parse_json_list(r.get("dish_type")))
        meal_type = " ".join(_parse_json_list(r.get("meal_type")))
        ing_objs = _parse_json_list(r.get("ingredients"))
        ingredients = [o.get("food") for o in ing_objs if isinstance(o, dict) and o.get("food")]
        if not ingredients:
            ingredients = _parse_json_list(r.get("ingredient_lines"))
        serves = _int(r.get("servings"), 0) or 8
        price, pp = _synth_price(rid, serves)
        desc = f"{name}. {cuisine or ''} {dish_type}".strip()
        return [MenuItem(
            id=f"datahive-{rid}", name=name, description=desc,
            cuisine=cuisine, course=_course_from(dish_type + " " + meal_type),
            serves=serves, price=price, price_pp=pp,
            caterer_name=_synth_caterer(rid), source="hf:datahive",
            ingredients_raw=[str(i) for i in ingredients], dietary_declared=declared,
        )]
