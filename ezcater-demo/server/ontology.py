"""
The Knowledge Graph tab's engine: a TBox store, a materializer, and a query layer.

Three ideas, in order:

  1. A MODEL (a TBox) is a *type-level* description of a graph: entity TYPES (`dish`,
     `cuisine`, `allergen`, ...) and relation TYPES (`HAS_CUISINE`, `SUITABLE_FOR`, ...).
     The user draws it on a canvas. It is a small JSON document (§B) and nothing more.

  2. MATERIALIZE compiles that model into an ABox — a real networkx DiGraph built by
     projecting every row of data/dishes.jsonl through the model's bindings. 600 dishes
     become 815 nodes and 11 429 edges in ~11 ms. Because it is one pass over the corpus
     and never chases an edge, a cycle in the *type* graph costs nothing.

  3. QUERY is set algebra over an inverted index of that graph. `diet:vegan cuisine:Italian`
     is one frozenset intersection. There is no traversal at query time, which is why a
     query answers in under 2 ms and why this whole feature works with Vespa switched off.

NOTHING here talks to Vespa, to an LLM, or to the network. Pure Python + networkx. That is
the point: the ontology tab is the part of the demo that keeps working when the cluster is
down, and it is the part that shows the *shape* of a constraint rather than just its result.

Node ids and attributes deliberately mirror ingest/graph.py (`kind:name`, attrs `kind`/`name`/
`label`/`n`) so the existing vis-network renderers draw this graph unmodified. The two graphs
are never merged, though — the food ontology and the model graph are disjoint worlds with
disjoint endpoints, sharing only a renderer.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field as _dc_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import networkx as nx

# uvicorn runs with cwd == server/, so the repo root is not implicitly importable. Put it on
# the path before reaching for the ingest package (server/main.py does the same dance).
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ═══════════════════════════════════════════════════════════════════════════════════════
# THE VOCABULARY SEAM
#
# The derivations below (dish_type, meal_type, diet closure, ingredient normalization) are
# owned by ingest/dish_type.py and ingest/vocab.py. This module PREFERS those and falls back
# to the transcriptions kept here when the package is unavailable — a fresh checkout that has
# not run the ingest layer, or a partial deploy, must still be able to materialize a graph.
#
# The fallbacks are line-for-line the same algorithms, verified against the same 600 rows
# (31 dish_type leaves, 7 groups, zero unmatched). If the two ever disagree the ingest package
# wins, because it is also what the coverage report and the slot typeahead read.
# ═══════════════════════════════════════════════════════════════════════════════════════

DISH_TYPE_TREE: dict[str, list[str]] = {
    "handheld":       ["pizza", "taco", "burrito", "wrap", "sandwich", "burger", "flatbread"],
    "grain_dish":     ["pasta", "noodles", "rice", "sushi"],
    "soup_and_stew":  ["soup", "curry", "chili"],
    "salad_and_bowl": ["salad", "grain_bowl"],
    "small_plate":    ["dumpling", "skewer", "fritter", "dip", "fried_bite", "platter"],
    "main_plate":     ["poultry", "beef", "pork", "seafood", "tofu", "egg_dish", "vegetable", "potato"],
    "sweet":          ["cake", "cookie", "pastry", "pie", "frozen_dessert", "chocolate", "custard", "fruit"],
    "beverage":       ["coffee", "tea", "juice", "alcohol"],
}
PARENT_OF: dict[str, str] = {leaf: p for p, leaves in DISH_TYPE_TREE.items() for leaf in leaves}
PARENT_OF["other"] = "other"
DISH_TYPE_LEAVES: list[str] = [l for ls in DISH_TYPE_TREE.values() for l in ls] + ["other"]
DISH_TYPE_GROUPS: list[str] = list(DISH_TYPE_TREE) + ["other"]

# Order is semantics, not preference: dish FORM beats preparation beats protein beats raw
# ingredient. "Vegetable Tempura" is a fried bite, not a vegetable; "Chicken Teriyaki Bowl"
# is a rice dish, not poultry. Reordering these lines changes what the graph asserts.
DISH_TYPE_RULES: list[tuple[str, str]] = [
    ("sushi",          r"sushi|sashimi|maki roll|maki|nigiri|poke bowl|poke|temaki|chirashi"),
    ("burrito",        r"burrito|quesadilla|enchilada|chimichanga|taquito|flauta"),
    ("taco",           r"tacos?|taquito"),
    ("pizza",          r"pizza|margherita|calzone|stromboli"),
    ("pasta",          r"pasta|spaghetti|linguine|fettuccine|penne|lasagna|lasagne|carbonara|bolognese|gnocchi|mac and cheese|mac & cheese|macaroni|alfredo|ravioli|orzo|tortellini|ziti|rigatoni"),
    ("noodles",        r"ramen|pho|noodles?|lo mein|pad thai|udon|soba|chow mein|vermicelli|glass noodle"),
    ("dumpling",       r"dumplings?|gyoza|potstickers?|empanadas?|samosas?|wontons?|pierogi|bao|momo|ravioli"),
    ("burger",         r"burgers?|cheeseburgers?|sliders?|patty melt"),
    ("wrap",           r"wraps?|shawarma|gyro|spring rolls?|summer rolls?|burrito wrap|lettuce wrap|roll ?up"),
    ("sandwich",       r"sandwich(es)?|panini|sub sandwich|hoagie|baguette|bagels?|club sandwich|blt|hot dogs?|corn dogs?|sliders? bun|croissant sandwich|toast bar|avocado toast"),
    ("skewer",         r"skewers?|satay|kebabs?|kabobs?|brochette|yakitori|souvlaki|anticucho"),
    ("fritter",        r"falafel|croquettes?|arancini|hush ?pupp(y|ies)|fritters?|latkes?|pakora"),
    ("flatbread",      r"naan|roti|chapati|focaccia|lavash|flatbreads?|pita bread|pita pockets?|crostini|bruschetta"),
    ("platter",        r"platters?|mezze|meze|charcuterie|cheese ?boards?|boards?|assort(ed|ment)|samplers?|spreads?|grazing|antipast(o|i)|crudit(e|es|é|és)"),
    ("dip",            r"hummus|guacamole|salsa|queso|baba ?ganoush|tzatziki|dips?|chips? and|chips? &|nachos?|tahini dip|pimento"),
    ("chili",          r"chili con carne|chilli con carne|\bchili\b(?!\s*(pepper|flake|oil|crisp|sauce|garlic|lime|paste))"),
    ("curry",          r"currys?|curries|curry|masala|tikka|korma|vindaloo|\bdal\b|\bdaal\b|butter chicken|rogan josh|tagine|jambalaya|mole"),
    ("soup",           r"soups?|bisque|chowder|broth|stews?|stewed|gumbo|minestrone|tom yum|congee|ramen broth|pozole|menudo"),
    ("rice",           r"fried rice|risotto|paella|pilaf|biryani|sticky rice|jollof|rice bowls?|arroz|steamed rice|coconut rice|\brice\b"),
    ("grain_bowl",     r"buddha bowls?|grain bowls?|power bowls?|quinoa bowls?|acai bowls?|farro bowls?|harvest bowls?|protein bowls?"),
    ("salad",          r"salads?|slaw|coleslaw|caesar|cobb|tabbouleh|tabouleh|caprese|nicoise|panzanella|greens?\b"),
    ("fried_bite",     r"tempura|wings?|nuggets?|poppers?|onion rings?|fried pickles?|calamari|karaage|popcorn chicken"),
    ("seafood",        r"shrimps?|prawns?|lobsters?|crabs?|crawfish|oysters?|clams?|mussels?|scallops?|salmon|tuna|\bcod\b|halibut|tilapia|trout|fish|seafood|ceviche|anchov(y|ies)|calamari|squid"),
    ("poultry",        r"chicken|turkey|poultry|duck|drumsticks?|rotisserie"),
    ("pork",           r"pork|bacon|sausages?|chorizo|\bham\b|prosciutto|pastrami|ribs|bratwurst|carnitas|pancetta|salami|pepperoni"),
    ("beef",           r"steaks?|beef|brisket|barbacoa|ribeye|sirloin|filet|carne|\bbbq\b|barbecue|pulled pork|lamb|veal|meatballs?|kofta|meatloaf"),
    ("tofu",           r"tofu|tempeh|seitan|edamame|paneer"),
    ("egg_dish",       r"omelet(te)?s?|frittatas?|quiches?|scrambles?|benedict|deviled eggs?|\beggs?\b|shakshuka"),
    ("potato",         r"potatoes?|potato|fries|mashed|hash browns?|tater"),
    ("vegetable",      r"broccoli|cauliflowers?|asparagus|brussels|kale|spinach|vegetables?|veggies?|carrots?|zucchini|eggplants?|aubergine|ratatouille|mushrooms?|portobello|roasted veg|grilled veg|corn|elote|beans?|chickpeas?|lentils?|avocados?"),
    ("frozen_dessert", r"ice ?cream|gelato|sorbet|sundae|mochi|popsicle|frozen yogurt|semifreddo"),
    ("cake",           r"cakes?|cupcakes?|cheesecakes?|tiramisu|brownies?|blondies?|torte|shortcake"),
    ("pie",            r"pies?|tarts?|cobblers?|galette|crumble|crisp\b"),
    ("cookie",         r"cookies?|biscotti|macarons?|shortbread|snickerdoodle|wafers?"),
    ("pastry",         r"pastr(y|ies)|croissants?|danish|scones?|cinnamon rolls?|churros?|baklava|strudel|donuts?|doughnuts?|beignet|muffins?|gulab jamun|jalebi|cannoli|eclair|profiterole|sticky buns?|pancakes?|waffles?|french toast|crepes?"),
    ("chocolate",      r"chocolates?|fudge|truffles?|ganache|brownie bites?"),
    ("custard",        r"custard|flan|puddings?|panna cotta|creme brulee|crème brûlée|mousse|tres leches|rice pudding|kheer|sticky rice"),
    ("fruit",          r"fruit|berr(y|ies)|strawberr(y|ies)|parfaits?|melon|acai|compote|fruit cups?"),
    ("coffee",         r"coffee|espresso|latte|cappuccino|mocha|cold brew"),
    ("tea",            r"matcha|green tea|chai|iced tea|\btea\b"),
    ("juice",          r"juice|smoothies?|lemonade|sodas?|milkshakes?|shakes?|agua fresca|horchata"),
    ("alcohol",        r"beer|ale\b|lager|\bipa\b|wine|sangria|prosecco|champagne|cocktails?|margaritas?|mojitos?|martinis?"),
]

# The boundary wraps the WHOLE alternation. App.jsx's FOOD_RULES use `\b(...)`, a leading
# boundary only, which is why "eggplant" matches "egg" there. That single asymmetry is the
# difference between Eggplant Caponata being a vegetable and being an egg dish.
_DT_COMPILED = [(leaf, re.compile(r"(?<![a-z])(?:" + pat + r")(?![a-z])", re.I))
                for leaf, pat in DISH_TYPE_RULES]
_BOWL_RE = re.compile(r"\bbowls?\b", re.I)


def _fb_dish_type(name: str, description: str = "", course: str = "") -> str:
    n, d, c = (name or "").lower(), (description or "").lower(), (course or "").lower()
    both = n + " " + d
    if c == "dessert":
        # Dessert guard: "Gulab Jamun — milk-dough DUMPLINGS soaked in syrup" is a pastry.
        eligible = [(l, r) for l, r in _DT_COMPILED if PARENT_OF.get(l) == "sweet"]
    else:
        eligible = _DT_COMPILED
        if _BOWL_RE.search(n):
            # A bare "bowl" is otherwise eaten by the `salad` rule. Route on what's IN the bowl.
            if "burrito" in n:
                return "burrito"
            if re.search(r"rice|teriyaki|poke|donburi|sushi", both):
                return "rice"
            if re.search(r"noodle|ramen|pho|udon", both):
                return "noodles"
            if re.search(r"soup|broth|stew|chowder", both):
                return "soup"
            return "grain_bowl"
    for leaf, rx in eligible:            # name alone first — the title is the strongest signal
        if rx.search(n):
            return leaf
    for leaf, rx in eligible:            # then let the description break the tie
        if rx.search(both):
            return leaf
    return "custard" if c == "dessert" else "other"


def _fb_dish_type_group(leaf: str) -> str:
    return PARENT_OF.get(leaf, "other")


MEAL_TYPES: list[str] = ["breakfast", "brunch", "lunch", "dinner", "snack", "dessert"]
# These four leave `occasion` entirely: they are times of day, not situations. `morning` is
# dropped as a synonym of `breakfast`, which is why occasion goes 12 -> 8 values.
MEAL_TIME_OCCASIONS: frozenset[str] = frozenset({"breakfast", "morning", "lunch", "dinner"})
OCCASIONS: list[str] = ["celebration", "client", "comfort", "healthy",
                        "impressive", "light", "team", "treat"]
COURSES: list[str] = ["main", "appetizer", "dessert"]
FLAVORS: list[str] = ["savory", "sweet", "fresh", "spicy"]
SPICE_LEVELS: list[tuple[int, str]] = [(0, "mild"), (1, "mild+"), (2, "spicy"), (3, "very spicy")]
CUISINES: list[str] = ["American", "Breakfast", "Chinese", "Indian", "Italian", "Japanese",
                       "Mediterranean", "Mexican", "Salads & Bowls", "Thai"]
# `Breakfast` is really a meal_type and `Salads & Bowls` is really a dish family. We do NOT
# rewrite fields.cuisine — /api/search, the Vespa dish schema and CUISINE_FEATURES all key off
# these exact strings — we flag the two nodes so the canvas can dim them.
SURROGATE_CUISINES: frozenset[str] = frozenset({"Breakfast", "Salads & Bowls"})

BREAKFAST_RE = re.compile(
    r"\b(breakfast|brunch|bagel|pancake|waffle|omelet|omelette|frittata|parfait|"
    r"granola|croissant|muffin|scone|danish|toast|oatmeal|benedict|hash brown|"
    r"cinnamon roll|coffee|espresso|latte|yogurt|acai|smoothie)\b", re.I)
SNACKABLE: frozenset[str] = frozenset({"dip", "platter", "fried_bite", "skewer", "cookie",
                                       "fruit", "chocolate", "dumpling", "fritter"})


def _fb_meal_type(fields: dict, dtype: str) -> list[str]:
    occ = set(fields.get("occasion") or [])
    course = (fields.get("course") or "").lower()
    cui = fields.get("cuisine") or ""
    pp = float(fields.get("price_pp") or 0)
    text = f"{fields.get('name', '')} {fields.get('description', '')}"
    out: set[str] = set()

    is_bk = (cui == "Breakfast" or course == "breakfast"
             or "breakfast" in occ or "morning" in occ
             or bool(BREAKFAST_RE.search(text)))

    if course == "dessert":
        out.add("dessert")
    if is_bk:
        out.add("breakfast")
    if is_bk and (pp >= 14 or "brunch" in text.lower()
                  or (occ & {"celebration", "impressive", "client"})):
        out.add("brunch")
    if "lunch" in occ:
        out.add("lunch")
    if "dinner" in occ:
        out.add("dinner")
    if course in ("appetizer", "dessert") and dtype in SNACKABLE:
        out.add("snack")
    if not (out & {"lunch", "dinner"}) and course == "main" and not is_bk:
        out.add("lunch")
        if pp >= 20 or (occ & {"impressive", "client"}):
            out.add("dinner")
    if not out:
        out.add("lunch")
    return sorted(out)


def _fb_occasions(fields: dict) -> list[str]:
    return sorted(o for o in (fields.get("occasion") or []) if o not in MEAL_TIME_OCCASIONS)


DIET_TAGS: list[str] = ["vegan", "vegetarian", "pescatarian", "gluten-free", "dairy-free",
                        "nut-free", "egg-free", "soy-free", "shellfish-free",
                        "halal", "kosher", "keto", "paleo"]
ALLERGENS: list[str] = ["gluten", "dairy", "eggs", "nuts", "peanuts",
                        "soy", "shellfish", "fish", "sesame"]
try:  # keep the allergen vocabulary single-sourced from the curated taxonomy when available
    from ingest.taxonomy import ALLERGENS as _TAX_ALLERGENS  # noqa: E402
    if _TAX_ALLERGENS:
        ALLERGENS = list(_TAX_ALLERGENS)
except Exception:  # noqa: BLE001 — the taxonomy is a nicety here, not a dependency
    pass


def _fb_derive_diet(dietary: list[str], allergens: list[str], ingredients: list[str]) -> list[str]:
    d, a, ing = set(dietary or []), set(allergens or []), set(ingredients or [])
    if not (a & {"nuts", "peanuts"}):
        d.add("nut-free")
    if "shellfish" not in a:
        d.add("shellfish-free")
    if "soy" not in a:
        d.add("soy-free")
    if "eggs" not in a and "egg" not in ing:
        d.add("egg-free")
    # The closure runs HERE, not at query time. Applying it at derivation is what makes the
    # coverage matrix honest: "Thai x vegetarian = 30", not a misleading 0 that only a
    # query-time expansion would have fixed.
    if "vegan" in d:
        d.add("vegetarian")
    return sorted(d)


# Dish FORMS masquerading as ingredients. dish_type() already captures them, and "falafel is
# an ingredient of Falafel Wrap" is exactly the noise this feature exists to delete.
INGREDIENT_STOPLIST: frozenset[str] = frozenset({
    "bagel", "brioche", "carnitas", "crouton", "curry", "dough", "falafel",
    "guacamole", "hummus", "icing", "ladyfinger", "naan", "phyllo", "pita",
    "roll", "salsa", "sushi", "teriyaki", "tortilla", "wrap",
})
# No-ops on today's 600 rows; load-bearing the moment the dataset is amplified.
INGREDIENT_MERGE: dict[str, str] = {
    "eggs": "egg", "noodles": "noodle", "peanuts": "peanut", "olives": "olive",
    "berries": "berry", "beans": "bean", "potatoes": "potato",
    "tomatoes": "tomato", "chickpeas": "chickpea", "penne": "pasta",
}


def _fb_normalize_ingredients(raw: Iterable[str]) -> list[str]:
    return sorted({INGREDIENT_MERGE.get(i, i) for i in (raw or [])
                   if i and i not in INGREDIENT_STOPLIST})


def _fb_price_band(price_pp: float | None) -> str:
    v = float(price_pp or 0)
    return "budget" if v < 12 else ("moderate" if v < 22 else "premium")


def _fb_serving_size(serves: int | None) -> str:
    v = int(serves or 0)
    return "small group" if v <= 10 else ("medium group" if v <= 25 else "large group")


def _fb_spice_label(level: int | None) -> str:
    v = int(level or 0)
    for lo, lab in SPICE_LEVELS:
        if v <= lo:
            return lab
    return SPICE_LEVELS[-1][1]


# The corpus is 60 base dishes x 9-10 caterers. Rolling up on this is what stops a result page
# from being twelve copies of Vegetable Tempura. Do NOT strip "Fresh" or "Assorted" — those
# belong to real names (Fresh Fruit Platter, Assorted Sushi Platter).
_VARIANT_PREFIX = re.compile(
    r"^(?:build-your-own|family-style|party-size|executive|classic|deluxe|office|premium)\s+")


def _fb_base_name(name: str) -> str:
    s = re.sub(r"\s+", " ", (name or "").strip().casefold())
    while True:
        s2 = _VARIANT_PREFIX.sub("", s)
        if s2 == s:
            return s
        s = s2


# ---- bind the seam: ingest package wins, transcription is the safety net ----------------
try:
    from ingest import dish_type as _dt_mod  # noqa: E402
except Exception:  # noqa: BLE001
    _dt_mod = None
try:
    from ingest import vocab as _vocab_mod  # noqa: E402
except Exception:  # noqa: BLE001
    _vocab_mod = None


def _bind(mod: Any, name: str, fallback: Callable) -> Callable:
    fn = getattr(mod, name, None) if mod is not None else None
    return fn if callable(fn) else fallback


dish_type: Callable[..., str] = _bind(_dt_mod, "dish_type", _fb_dish_type)
dish_type_group: Callable[[str], str] = _bind(_dt_mod, "dish_type_group", _fb_dish_type_group)
meal_type: Callable[[dict, str], list[str]] = _bind(_vocab_mod, "meal_type", _fb_meal_type)
occasions: Callable[[dict], list[str]] = _bind(_vocab_mod, "occasions", _fb_occasions)
derive_diet: Callable[..., list[str]] = _bind(_vocab_mod, "derive_diet", _fb_derive_diet)
normalize_ingredients: Callable[[Iterable[str]], list[str]] = _bind(
    _vocab_mod, "normalize_ingredients", _fb_normalize_ingredients)
price_band: Callable[[Any], str] = _bind(_vocab_mod, "price_band", _fb_price_band)
serving_size: Callable[[Any], str] = _bind(_vocab_mod, "serving_size", _fb_serving_size)
spice_label: Callable[[Any], str] = _bind(_vocab_mod, "spice_label", _fb_spice_label)
base_name: Callable[[str], str] = _bind(_vocab_mod, "base_name", _fb_base_name)

if _dt_mod is not None:  # keep the exported taxonomy identical to whatever is actually in use
    DISH_TYPE_TREE = getattr(_dt_mod, "DISH_TYPE_TREE", DISH_TYPE_TREE)
    DISH_TYPE_LEAVES = getattr(_dt_mod, "DISH_TYPE_LEAVES", DISH_TYPE_LEAVES)
    DISH_TYPE_GROUPS = getattr(_dt_mod, "DISH_TYPE_GROUPS", DISH_TYPE_GROUPS)
    PARENT_OF = getattr(_dt_mod, "PARENT_OF", PARENT_OF)

VOCAB_SOURCE = "ingest" if (_dt_mod is not None and _vocab_mod is not None) else "embedded"


def project(fields: dict) -> dict:
    """dishes.jsonl `fields` -> the full node-ready projection.

    The ONE place where "what the data says" becomes "what the graph contains". The
    materializer, the slot typeahead and the coverage report all read from here, so the
    closed vocabularies can never drift apart. Pure — no I/O, no LLM, no network.
    """
    if _vocab_mod is not None and callable(getattr(_vocab_mod, "project", None)):
        return _vocab_mod.project(fields)
    dt = dish_type(fields.get("name", ""), fields.get("description", ""), fields.get("course", ""))
    ings_raw = list(fields.get("ingredients") or [])
    return {
        "dish_type": dt,
        "dish_type_group": dish_type_group(dt),
        "meal_type": meal_type(fields, dt),
        "occasion": occasions(fields),
        "ingredient": normalize_ingredients(ings_raw),
        "diet": derive_diet(list(fields.get("dietary") or []),
                            list(fields.get("allergens") or []), ings_raw),
        "allergen": sorted(set(fields.get("allergens") or [])),
        "cuisine": fields.get("cuisine") or "",
        "course": fields.get("course") or "",
        "flavor": fields.get("flavor") or "",
        "price_band": price_band(fields.get("price_pp")),
        "serving_size": serving_size(fields.get("serves")),
        "caterer": fields.get("caterer_name") or "",
        "spice_level": int(fields.get("spice_level") or 0),
    }


# ═══════════════════════════════════════════════════════════════════════════════════════
# CONSTANTS, PATHS, LIMITS
# ═══════════════════════════════════════════════════════════════════════════════════════

SCHEMA_VERSION: int = 1
# Hand-bumped. The ONLY invalidation channel for a *code* change: bump this and every cached
# ABox on disk is considered stale on the next read.
BUILDER_VERSION: int = 1

DATA_DIR: Path = _ROOT / "data"
MODELS_DIR: Path = DATA_DIR / "kg_models"      # TBoxes — source of truth, committed
GRAPHS_DIR: Path = DATA_DIR / "kg_graphs"      # ABoxes — derived cache, gitignored
DATASET_PATH: Path = DATA_DIR / "dishes.jsonl"
ACTIVE_PATH: Path = MODELS_DIR / "_active.json"
QUERIES_PATH: Path = DATA_DIR / "kg_queries.json"

# An allow-list of exactly one in v1. `dataset` is a user-supplied string on a saved model, so
# it is a path-traversal surface; resolving it through a dict rather than joining is the fix.
ALLOWED_DATASETS: dict[str, Path] = {"data/dishes.jsonl": DATASET_PATH}

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
TAG_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
REL_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,39}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

NORM_DEFAULT: dict = {"case": "lower", "trim": True, "collapse_ws": True,
                      "dedupe": True, "max_len": 80}
DROP_VALUES: frozenset[str] = frozenset(
    {"", "n/a", "na", "none", "null", "unknown", "other", "misc", "-"})

SAMPLE_DOCS: int = 50
MAX_ENTITY_TYPES: int = 40
MAX_RELATION_TYPES: int = 120
MAX_EDGES_PER_RELATION: int = 50_000
MAX_TOTAL_EDGES: int = 400_000
MAX_HOPS: int = 3
TOP_VALUES: int = 8
MAX_ORPHANS_LISTED: int = 20

# The card wants "Italian", not "italian", and short keys. Payload values are copied RAW.
PAYLOAD_ALIASES: dict[str, str] = {"description": "desc", "caterer_name": "caterer"}
PAYLOAD_DESC_LEN: int = 200

# A dish_type LEAF -> the JPEG that already exists in web/public/food/. This direction is
# safe; renaming the JPEGs is not, because App.jsx's FOOD_RULES also address them.
PHOTO_KEY: dict[str, str] = {
    "pizza": "pizza", "taco": "taco", "burrito": "burrito", "wrap": "wrap",
    "sandwich": "sandwich", "burger": "burger", "flatbread": "wrap",
    "pasta": "pasta", "noodles": "noodles", "rice": "rice", "sushi": "sushi",
    "soup": "soup", "curry": "curry", "chili": "chili",
    "salad": "salad", "grain_bowl": "salad",
    "dumpling": "dumpling", "skewer": "chicken", "fritter": "falafel", "dip": "avocado",
    "fried_bite": "shrimp", "platter": "platter",
    "poultry": "chicken", "beef": "beef", "pork": "pork", "seafood": "shrimp",
    "tofu": "tofu", "egg_dish": "egg", "vegetable": "veg", "potato": "potato",
    "cake": "cake", "cookie": "cookie", "pastry": "croissant", "pie": "pie",
    "frozen_dessert": "icecream", "chocolate": "chocolate", "custard": "custard", "fruit": "fruit",
    "coffee": "coffee", "tea": "tea", "juice": "juice", "alcohol": "wine",
    # dish FAMILIES, so a group node can be illustrated too; "other" falls through to platter
    "handheld": "sandwich", "grain_dish": "rice", "soup_and_stew": "soup",
    "salad_and_bowl": "salad", "small_plate": "platter", "main_plate": "chicken",
    "sweet": "cake", "beverage": "coffee", "other": "platter",
}

# Exclusions on these tags are treated as SAFETY claims, not preferences: an unresolved value
# blocks the query rather than silently answering it, and the relaxation ladder never offers
# to drop one. `ingredient` and `diet` can be promoted per-deployment; allergen never demotes.
SAFETY_CRITICAL_TAGS: frozenset[str] = frozenset({"allergen"})


class OntologyError(Exception):
    """Structured failure the HTTP layer can turn straight into an error envelope."""

    def __init__(self, code: str, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


# ═══════════════════════════════════════════════════════════════════════════════════════
# SMALL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════════════

def utcnow_iso() -> str:
    """Call this from a REQUEST HANDLER, never at module scope — a timestamp frozen at import
    would stamp every save of a long-lived process with the boot time."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _collapse_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s)


def _slug(text: str) -> str:
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:48] or "model"


def _rel_name(text: str) -> str:
    """'is served at' -> 'IS_SERVED_AT'. Relation names are invented by the user at connect
    time, so this is the only guard between a free-text gesture and a stable graph key."""
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").upper()
    if s and s[0].isdigit():
        s = "R_" + s
    return s[:40] or "RELATED_TO"


def _norm(value: Any, spec: dict | None = None) -> str:
    """Normalize a raw cell into a node NAME. Order matters: str -> collapse_ws -> trim ->
    case -> max_len. Returns '' for anything that should be discarded."""
    sp = spec or NORM_DEFAULT
    s = "" if value is None else str(value)
    if sp.get("collapse_ws", True):
        s = _collapse_ws(s)
    if sp.get("trim", True):
        s = s.strip()
    case = sp.get("case", "lower")
    if case == "lower":
        s = s.lower()
    elif case == "upper":
        s = s.upper()
    ml = int(sp.get("max_len", 80) or 80)
    if ml > 0:
        s = s[:ml]
    return s


def _vkey(value: Any) -> str:
    """Lookup key for a user-typed value. Must agree with _norm's default output or the query
    layer silently fails to resolve values the materializer happily created."""
    return _collapse_ws(str(value or "").strip()).lower()


def _write_json_atomic(path: Path, obj: Any) -> None:
    """tmp -> os.replace. A half-written model on disk is a 500 on every subsequent request;
    os.replace is atomic within a filesystem, so a crash leaves either the old file or the new."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# keys that describe presentation or bookkeeping, not structure — dragging a node on the canvas
# must NOT invalidate an 11 429-edge build
_HASH_EXCLUDE = ("updated_at", "version", "layout", "name", "description", "stats", "materialized")


def _canonical_model(model: dict) -> dict:
    m = {k: v for k, v in (model or {}).items() if k not in _HASH_EXCLUDE}
    ents = sorted((dict(e) for e in m.get("entity_types") or []), key=lambda e: e.get("tag", ""))
    rels = sorted((dict(r) for r in m.get("relation_types") or []),
                  key=lambda r: (r.get("rel", ""), r.get("from", ""), r.get("to", "")))
    for e in ents:
        e.pop("color", None)
        e.pop("icon", None)
        e.pop("label", None)
        e.pop("plural", None)
    for r in rels:
        for k in ("color", "dashes", "label", "inverse_label", "user_created"):
            r.pop(k, None)
    m["entity_types"] = ents
    m["relation_types"] = rels
    return m


def model_hash(model: dict) -> str:
    blob = json.dumps(_canonical_model(model), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def dataset_hash(path: Path = DATASET_PATH) -> str:
    """sha256 of the whole file. NEVER mtime — data/build_dataset.py rewrites dishes.jsonl and
    a same-second rewrite would go unnoticed. 297 KB hashes in well under a millisecond."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return "missing"


def iter_docs(path: Path = DATASET_PATH) -> Iterator[dict]:
    """Yield each row's `fields` dict, with `id` guaranteed present (dishes.jsonl carries the id
    both outside and inside `fields`; other producers may not)."""
    p = Path(path)
    if not p.exists():
        raise OntologyError("DATASET_MISSING", f"Dataset not found: {p}", detail={"path": str(p)})
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            fields = row.get("fields") if isinstance(row, dict) else None
            if not isinstance(fields, dict):
                fields = row if isinstance(row, dict) else None
            if not isinstance(fields, dict):
                continue
            if "id" not in fields and isinstance(row, dict) and "id" in row:
                fields = {**fields, "id": row["id"]}
            yield fields


def dataset_fields(path: Path = DATASET_PATH, sample: int = 200) -> dict[str, dict]:
    """Field inventory for the builder's palette: which columns exist, scalar or list, how
    populated, and a few example values to show under a candidate entity type."""
    out: dict[str, dict] = {}
    n = 0
    for fields in iter_docs(path):
        n += 1
        for k, v in fields.items():
            slot = out.setdefault(k, {"field": k, "type": "scalar", "present": 0,
                                      "examples": [], "_seen": set()})
            if v is None or v == "" or v == []:
                continue
            slot["present"] += 1
            if isinstance(v, list):
                slot["type"] = "list"
                vals = v
            else:
                vals = [v]
            for x in vals:
                key = str(x)
                if len(slot["_seen"]) < 400:
                    slot["_seen"].add(key)
                if len(slot["examples"]) < 5 and key not in slot["examples"]:
                    slot["examples"].append(key)
        if n >= sample:
            break
    for slot in out.values():
        slot["distinct"] = len(slot.pop("_seen"))
        slot["coverage"] = round(slot["present"] / n, 4) if n else 0.0
        slot["sampled"] = n
    return out


# ═══════════════════════════════════════════════════════════════════════════════════════
# DERIVERS — the fourth binding
#
# All four bindings (doc / field / list_field / derived) converge on ONE code path because
# derive() always returns a list. A registry entry is either a Python callable taking the raw
# `fields` dict, or a declarative spec dict the user can author from the builder without code.
# ═══════════════════════════════════════════════════════════════════════════════════════

def _d_dish_type(fields: dict) -> list[str]:
    return [dish_type(fields.get("name", ""), fields.get("description", ""),
                      fields.get("course", ""))]


def _d_dish_type_group(fields: dict) -> list[str]:
    return [dish_type_group(dish_type(fields.get("name", ""), fields.get("description", ""),
                                      fields.get("course", "")))]


def _d_meal_type(fields: dict) -> list[str]:
    dt = dish_type(fields.get("name", ""), fields.get("description", ""), fields.get("course", ""))
    return list(meal_type(fields, dt))


def _d_occasion_split(fields: dict) -> list[str]:
    return list(occasions(fields))


def _d_diet(fields: dict) -> list[str]:
    return list(derive_diet(list(fields.get("dietary") or []),
                            list(fields.get("allergens") or []),
                            list(fields.get("ingredients") or [])))


def _d_ingredient_norm(fields: dict) -> list[str]:
    return list(normalize_ingredients(list(fields.get("ingredients") or [])))


DERIVERS: dict[str, Any] = {
    "dish_type":       _d_dish_type,
    "dish_type_group": _d_dish_type_group,
    "meal_type":       _d_meal_type,
    "occasion_split":  _d_occasion_split,
    "diet":            _d_diet,
    "ingredient_norm": _d_ingredient_norm,
    "price_band":   {"kind": "bucket", "field": "price_pp",
                     "buckets": [{"out": "budget", "lt": 12}, {"out": "moderate", "lt": 22},
                                 {"out": "premium"}]},
    "serving_size": {"kind": "bucket", "field": "serves",
                     "buckets": [{"out": "small group", "lte": 10},
                                 {"out": "medium group", "lte": 25},
                                 {"out": "large group"}]},
    # registered but unused by DEFAULT_MODEL — proof the registry is extensible from the canvas
    "spice_band":   {"kind": "bucket", "field": "spice_level",
                     "buckets": [{"out": "mild", "lte": 0}, {"out": "medium", "lte": 1},
                                 {"out": "hot"}]},
}


def resolve_deriver(binding: dict) -> dict | Callable:
    name = (binding or {}).get("deriver")
    spec = (binding or {}).get("spec")
    if name and spec:
        raise OntologyError("INVALID_MODEL",
                            "A derived binding takes exactly one of `deriver` or `spec`.",
                            detail={"binding": binding})
    if name:
        if name not in DERIVERS:
            raise OntologyError("INVALID_MODEL", f"Unknown deriver {name!r}.",
                                detail={"deriver": name, "known": sorted(DERIVERS)})
        return DERIVERS[name]
    if isinstance(spec, dict):
        return spec
    raise OntologyError("INVALID_MODEL", "A derived binding needs `deriver` or `spec`.",
                        detail={"binding": binding})


def _spec_rules(spec: dict, fields: dict) -> list[str]:
    src = " ".join(str(fields.get(f) or "") for f in (spec.get("fields") or ["name"]))
    flags = re.I if "i" in (spec.get("flags") or "i") else 0
    wb = bool(spec.get("word_boundary", True))
    multi = bool(spec.get("multi", False))
    out: list[str] = []
    for pair in spec.get("rules") or []:
        label, pattern = (pair + ["", ""])[:2] if isinstance(pair, list) else (pair, "")
        pat = r"(?<![a-z])(?:" + pattern + r")(?![a-z])" if wb else pattern
        try:
            rx = re.compile(pat, flags)
        except re.error:
            continue
        if rx.search(src):
            out.append(label)
            if not multi:
                return out
    if out:
        return out
    fb = spec.get("fallback")
    if fb in (None, "", []):
        return []
    return list(fb) if isinstance(fb, list) else [str(fb)]


def _spec_map(spec: dict, fields: dict) -> list[str]:
    multi = bool(spec.get("multi", True))
    out: list[str] = []
    for rule in spec.get("rules") or []:
        conds = rule.get("any_of") or []
        hit = False
        for cond in conds:
            raw = fields.get(cond.get("field"))
            have = {_vkey(x) for x in (raw if isinstance(raw, list) else [raw]) if x not in (None, "")}
            want = {_vkey(x) for x in (cond.get("in") or [])}
            if have & want:
                hit = True
                break
        if hit:
            out.append(rule.get("out"))
            if not multi:
                return out
    if out:
        return out
    fb = spec.get("fallback")
    if fb in (None, "", []):
        return []
    return list(fb) if isinstance(fb, list) else [str(fb)]


def _spec_bucket(spec: dict, fields: dict) -> list[str]:
    raw = fields.get(spec.get("field"))
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return []
    for b in spec.get("buckets") or []:
        if "lt" in b:
            if v < float(b["lt"]):
                return [b.get("out")]
        elif "lte" in b:
            if v <= float(b["lte"]):
                return [b.get("out")]
        else:
            return [b.get("out")]        # the bound-less catch-all, which must be last
    return []


def derive(spec: dict | Callable, fields: dict) -> list[str]:
    """ALWAYS returns a list, so the materializer never branches on binding kind."""
    if callable(spec):
        out = spec(fields)
    elif isinstance(spec, dict):
        kind = spec.get("kind")
        if kind == "rules":
            out = _spec_rules(spec, fields)
        elif kind == "map":
            out = _spec_map(spec, fields)
        elif kind == "bucket":
            out = _spec_bucket(spec, fields)
        else:
            out = []
    else:
        out = []
    if out is None:
        return []
    if isinstance(out, (str, int, float)):
        return [str(out)]
    return [x for x in out if x not in (None, "")]


# ═══════════════════════════════════════════════════════════════════════════════════════
# DEFAULT_MODEL — the built-in TBox, virtual until the user edits it
# ═══════════════════════════════════════════════════════════════════════════════════════

# Timestamps are FROZEN literals, not utcnow(): DEFAULT_MODEL is a constant, and a moving
# created_at would make model_hash unstable and every cached graph permanently stale.
DEFAULT_MODEL: dict = {
    "schema_version": 1,
    "id": "catering-core",
    "name": "Catering Core",
    "description": ("Dish-centred catering ontology over data/dishes.jsonl. Every dish becomes "
                    "a node; its cuisine, dish type, ingredients, dietary capabilities, "
                    "allergens, meal times, occasions, course, flavor, price band, serving size "
                    "and caterer become shared concept nodes."),
    "version": 1,
    "created_at": "2026-08-25T00:00:00Z",
    "updated_at": "2026-08-25T00:00:00Z",
    "dataset": "data/dishes.jsonl",
    "builtin": True,
    "materialized": False,
    "stats": None,

    "entity_types": [
        {"tag": "dish", "label": "Dish", "plural": "Dishes", "color": "#e35205", "icon": "🍽️",
         "binding": {"source": "doc", "id_field": "id", "label_field": "name",
                     "payload_fields": ["id", "name", "description", "cuisine", "caterer_name",
                                        "price", "price_pp", "serves", "popularity", "course",
                                        "spice_level"]}},
        {"tag": "cuisine", "label": "Cuisine", "color": "#00695c", "icon": "🌍",
         "binding": {"source": "field", "field": "cuisine"}},
        {"tag": "dish_type", "label": "Dish type", "color": "#d81b60", "icon": "🥘",
         "binding": {"source": "derived", "deriver": "dish_type"}},
        {"tag": "dish_type_group", "label": "Dish family", "plural": "Dish families",
         "color": "#ad1457", "icon": "🗂️",
         "binding": {"source": "derived", "deriver": "dish_type_group"}},
        {"tag": "ingredient", "label": "Ingredient", "color": "#7c8698", "icon": "🥕",
         "binding": {"source": "derived", "deriver": "ingredient_norm"}},
        {"tag": "allergen", "label": "Allergen", "color": "#c62828", "icon": "⚠️",
         "binding": {"source": "list_field", "field": "allergens"}},
        {"tag": "diet", "label": "Dietary capability", "plural": "Dietary capabilities",
         "color": "#2e7d32", "icon": "🌱",
         "binding": {"source": "derived", "deriver": "diet"}},
        {"tag": "meal_type", "label": "Meal type", "color": "#1140d6", "icon": "🕐",
         "binding": {"source": "derived", "deriver": "meal_type"}},
        {"tag": "occasion", "label": "Occasion", "color": "#5b4b8a", "icon": "🎉",
         "binding": {"source": "derived", "deriver": "occasion_split"}},
        {"tag": "course", "label": "Course", "color": "#a15c00", "icon": "📋",
         "binding": {"source": "field", "field": "course"}},
        {"tag": "flavor", "label": "Flavor", "color": "#0277bd", "icon": "👅",
         "binding": {"source": "field", "field": "flavor"}},
        {"tag": "price_band", "label": "Price band", "color": "#00838f", "icon": "💲",
         "binding": {"source": "derived", "spec": {"kind": "bucket", "field": "price_pp",
                     "buckets": [{"out": "budget", "lt": 12}, {"out": "moderate", "lt": 22},
                                 {"out": "premium"}]}}},
        {"tag": "serving_size", "label": "Serving size", "color": "#6a1b9a", "icon": "👥",
         "binding": {"source": "derived", "spec": {"kind": "bucket", "field": "serves",
                     "buckets": [{"out": "small group", "lte": 10},
                                 {"out": "medium group", "lte": 25},
                                 {"out": "large group"}]}}},
        {"tag": "caterer", "label": "Caterer", "color": "#455a64", "icon": "🏪",
         "binding": {"source": "field", "field": "caterer_name"}},
    ],

    "relation_types": [
        {"rel": "HAS_CUISINE", "label": "is", "inverse_label": "is cuisine of",
         "from": "dish", "to": "cuisine", "cardinality": "many_to_one", "via": "doc",
         "color": "#00695c"},
        {"rel": "IS_DISH_TYPE", "label": "is a", "inverse_label": "has dish",
         "from": "dish", "to": "dish_type", "cardinality": "many_to_one", "via": "doc",
         "color": "#d81b60"},
        {"rel": "IN_DISH_FAMILY", "label": "in family", "inverse_label": "includes",
         "from": "dish", "to": "dish_type_group", "cardinality": "many_to_one", "via": "doc",
         "color": "#ad1457"},
        {"rel": "IS_A", "label": "is a kind of", "inverse_label": "has kind",
         "from": "dish_type", "to": "dish_type_group", "cardinality": "many_to_one",
         "via": "cooccurrence", "color": "#ad1457", "dashes": True},
        {"rel": "CONTAINS_INGREDIENT", "label": "contains", "inverse_label": "is in",
         "from": "dish", "to": "ingredient", "cardinality": "many_to_many", "via": "doc",
         "color": "#7c8698"},
        {"rel": "HAS_ALLERGEN", "label": "contains allergen", "inverse_label": "found in",
         "from": "dish", "to": "allergen", "cardinality": "many_to_many", "via": "doc",
         "color": "#c62828", "dashes": True},
        {"rel": "SUITABLE_FOR", "label": "suitable for", "inverse_label": "satisfied by",
         "from": "dish", "to": "diet", "cardinality": "many_to_many", "via": "doc",
         "color": "#2e7d32"},
        {"rel": "SERVED_AT", "label": "served at", "inverse_label": "served",
         "from": "dish", "to": "meal_type", "cardinality": "many_to_many", "via": "doc",
         "color": "#1140d6"},
        {"rel": "FITS_OCCASION", "label": "fits", "inverse_label": "suits",
         "from": "dish", "to": "occasion", "cardinality": "many_to_many", "via": "doc",
         "color": "#5b4b8a"},
        {"rel": "IS_COURSE", "label": "served as", "inverse_label": "course of",
         "from": "dish", "to": "course", "cardinality": "many_to_one", "via": "doc",
         "color": "#a15c00"},
        {"rel": "TASTES", "label": "tastes", "inverse_label": "flavor of",
         "from": "dish", "to": "flavor", "cardinality": "many_to_one", "via": "doc",
         "color": "#0277bd"},
        {"rel": "PRICED", "label": "priced", "inverse_label": "price of",
         "from": "dish", "to": "price_band", "cardinality": "many_to_one", "via": "doc",
         "color": "#00838f"},
        {"rel": "SERVES_GROUP", "label": "serves", "inverse_label": "size of",
         "from": "dish", "to": "serving_size", "cardinality": "many_to_one", "via": "doc",
         "color": "#6a1b9a"},
        {"rel": "OFFERED_BY", "label": "offered by", "inverse_label": "offers",
         "from": "dish", "to": "caterer", "cardinality": "many_to_one", "via": "doc",
         "color": "#455a64"},
    ],

    # a ring around the dish node — the builder opens on something that already reads as a model
    "layout": {
        "dish":            {"x": 0, "y": 0},
        "cuisine":         {"x": 340, "y": 0},
        "dish_type":       {"x": 301, "y": 158},
        "dish_type_group": {"x": 193, "y": 280},
        "ingredient":      {"x": 41, "y": 338},
        "allergen":        {"x": -121, "y": 318},
        "diet":            {"x": -254, "y": 225},
        "meal_type":       {"x": -330, "y": 81},
        "occasion":        {"x": -330, "y": -81},
        "course":          {"x": -254, "y": -225},
        "flavor":          {"x": -121, "y": -318},
        "price_band":      {"x": 41, "y": -338},
        "serving_size":    {"x": 193, "y": -280},
        "caterer":         {"x": 301, "y": -158},
    },
}

# Shipped so the builder can generate forms and validate a draft client-side without a round
# trip. It mirrors the prose in §B; validate_model() remains the authority.
ONTOLOGY_MODEL_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Knowledge Graph ontology model",
    "type": "object",
    "required": ["schema_version", "id", "name", "dataset", "entity_types"],
    "additionalProperties": True,
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "id": {"type": "string", "pattern": ID_RE.pattern},
        "name": {"type": "string", "minLength": 1, "maxLength": 80},
        "description": {"type": "string", "maxLength": 400},
        "version": {"type": "integer", "minimum": 1},
        "created_at": {"type": "string", "pattern": TS_RE.pattern},
        "updated_at": {"type": "string", "pattern": TS_RE.pattern},
        "dataset": {"enum": sorted(ALLOWED_DATASETS)},
        "builtin": {"type": "boolean"},
        "materialized": {"type": "boolean"},
        "stats": {"type": ["object", "null"]},
        "layout": {"type": "object", "additionalProperties": {
            "type": "object",
            "properties": {"x": {"type": "number"}, "y": {"type": "number"}}}},
        "entity_types": {
            "type": "array", "minItems": 1, "maxItems": MAX_ENTITY_TYPES,
            "items": {
                "type": "object", "required": ["tag", "label", "binding"],
                "properties": {
                    "tag": {"type": "string", "pattern": TAG_RE.pattern},
                    "label": {"type": "string", "minLength": 1, "maxLength": 40},
                    "plural": {"type": "string", "maxLength": 48},
                    "color": {"type": "string", "pattern": COLOR_RE.pattern},
                    "icon": {"type": "string", "maxLength": 4},
                    "normalize": {"type": "object"},
                    "drop_values": {"type": "array", "items": {"type": "string"}},
                    "binding": {
                        "type": "object", "required": ["source"],
                        "properties": {
                            "source": {"enum": ["doc", "field", "list_field", "derived"]},
                            "field": {"type": "string"},
                            "id_field": {"type": "string"},
                            "label_field": {"type": "string"},
                            "payload_fields": {"type": "array", "items": {"type": "string"}},
                            "deriver": {"enum": sorted(DERIVERS)},
                            "spec": {"type": "object"},
                        }}}}},
        "relation_types": {
            "type": "array", "maxItems": MAX_RELATION_TYPES,
            "items": {
                "type": "object", "required": ["rel", "from", "to"],
                "properties": {
                    "rel": {"type": "string", "pattern": REL_RE.pattern},
                    "label": {"type": "string", "minLength": 1, "maxLength": 40},
                    "inverse_label": {"type": "string", "maxLength": 40},
                    "from": {"type": "string", "pattern": TAG_RE.pattern},
                    "to": {"type": "string", "pattern": TAG_RE.pattern},
                    "cardinality": {"enum": ["one_to_one", "one_to_many",
                                             "many_to_one", "many_to_many"]},
                    "directed": {"type": "boolean"},
                    "via": {"enum": ["doc", "cooccurrence", "food_graph"]},
                    "min_support": {"type": "integer", "minimum": 1},
                    "max_edges": {"type": "integer", "minimum": 1},
                    "self_loops": {"type": "boolean"},
                    "color": {"type": "string", "pattern": COLOR_RE.pattern},
                    "dashes": {"type": "boolean"},
                    "user_created": {"type": "boolean"},
                }}},
    },
}


# ═══════════════════════════════════════════════════════════════════════════════════════
# MODEL CRUD
#
# DEFAULT_MODEL is VIRTUAL until edited: list_models() always includes it whether or not a
# file exists, so the tab is never empty on first open and the repo needs no seed file. A disk
# file with the same id shadows the constant and reports builtin: false.
# ═══════════════════════════════════════════════════════════════════════════════════════

def _model_path(model_id: str) -> Path:
    if not ID_RE.match(model_id or ""):
        raise OntologyError("BAD_ID", f"Invalid model id {model_id!r}.",
                            detail={"id": model_id, "pattern": ID_RE.pattern})
    return MODELS_DIR / f"{model_id}.json"   # id doubles as the filename; ID_RE is the guard


def _compact_binding(binding: dict) -> str:
    """Object form -> the compact string the builder palette speaks (`field:cuisine`)."""
    src = (binding or {}).get("source")
    if src == "doc":
        return "row"
    if src == "field":
        return f"field:{binding.get('field', '')}"
    if src == "list_field":
        return f"list:{binding.get('field', '')}"
    if src == "derived":
        return f"derive:{binding.get('deriver') or (binding.get('spec') or {}).get('kind', 'spec')}"
    return "none"


def expand_binding(compact: Any) -> dict:
    """Compact string -> object form. The only place the two representations meet; the router
    calls it on save so the canvas can stay on the short form end to end."""
    if isinstance(compact, dict):
        return compact
    s = str(compact or "none")
    if s == "row":
        return dict(DEFAULT_MODEL["entity_types"][0]["binding"])
    head, _, rest = s.partition(":")
    if head == "field":
        return {"source": "field", "field": rest}
    if head == "list":
        return {"source": "list_field", "field": rest}
    if head == "derive":
        return {"source": "derived", "deriver": rest}
    return {"source": "field", "field": rest or s}


def _doc_type(model: dict) -> dict | None:
    for e in model.get("entity_types") or []:
        if ((e.get("binding") or {}).get("source")) == "doc":
            return e
    return None


def doc_tag(model: dict) -> str:
    e = _doc_type(model)
    return e.get("tag", "dish") if e else "dish"


def list_models() -> list[dict]:
    """Summary rows for the model picker. Reads meta sidecars, NEVER loads a graph."""
    seen: dict[str, dict] = {}
    out: list[dict] = []

    def _summary(m: dict, builtin: bool) -> dict:
        mid = m.get("id", "")
        meta = _read_meta(mid)
        stale, reason = is_stale(mid, model=m)
        return {
            "id": mid, "name": m.get("name", mid), "description": m.get("description", ""),
            "version": int(m.get("version") or 1),
            "created_at": m.get("created_at", ""), "updated_at": m.get("updated_at", ""),
            "builtin": builtin,
            "entity_types": len(m.get("entity_types") or []),
            "relation_types": len(m.get("relation_types") or []),
            "materialized": bool(meta) and not stale,
            "stale": stale, "stale_reason": reason,
            "nodes": (meta.get("stats") or {}).get("nodes") if meta else None,
            "edges": (meta.get("stats") or {}).get("edges") if meta else None,
            "active": False,
        }

    if MODELS_DIR.exists():
        for p in sorted(MODELS_DIR.glob("*.json")):
            if p.name.startswith("_"):
                continue
            try:
                m = _read_json(p)
            except Exception:  # noqa: BLE001 — one unreadable file must not empty the picker
                continue
            if not isinstance(m, dict) or not m.get("id"):
                continue
            seen[m["id"]] = m
            out.append(_summary(m, builtin=False))
    if DEFAULT_MODEL["id"] not in seen:
        out.insert(0, _summary(DEFAULT_MODEL, builtin=True))

    active = active_model_id()
    for row in out:
        row["active"] = row["id"] == active
    return out


def load_model(model_id: str) -> dict:
    if model_id in ("default", ""):
        return json.loads(json.dumps(DEFAULT_MODEL))
    path = _model_path(model_id)
    if path.exists():
        try:
            m = _read_json(path)
        except json.JSONDecodeError as exc:
            raise OntologyError("INVALID_JSON", f"Model {model_id!r} is not valid JSON.",
                                detail={"error": str(exc)}) from exc
        if isinstance(m, dict):
            m.setdefault("builtin", False)
            m["materialized"] = not is_stale(model_id, model=m)[0]
            return m
    if model_id == DEFAULT_MODEL["id"]:
        m = json.loads(json.dumps(DEFAULT_MODEL))
        m["materialized"] = not is_stale(model_id, model=m)[0]
        return m
    raise OntologyError("NOT_FOUND", f"No ontology model named {model_id!r}.",
                        detail={"id": model_id})


def save_model(model: dict, *, now: str) -> dict:
    """Validate and persist. Deliberately does NOT materialize: the UI's "Save & materialize"
    is two calls so it can show two progress states, and a failed build never loses the canvas."""
    if not isinstance(model, dict):
        raise OntologyError("INVALID_MODEL", "Model must be a JSON object.")
    m = json.loads(json.dumps(model))
    m["schema_version"] = SCHEMA_VERSION
    m["id"] = m.get("id") or _slug(m.get("name") or "model")
    for e in m.get("entity_types") or []:
        if not isinstance(e.get("binding"), dict):
            e["binding"] = expand_binding(e.get("binding"))
    for r in m.get("relation_types") or []:
        r["rel"] = _rel_name(r.get("rel") or r.get("label") or "")
    m.setdefault("created_at", now)
    m["updated_at"] = now
    m.pop("materialized", None)                 # server-owned; never trust the client's copy
    m.pop("stats", None)

    prev_version = 0
    path = _model_path(m["id"])
    if path.exists():
        try:
            prev_version = int((_read_json(path) or {}).get("version") or 0)
        except Exception:  # noqa: BLE001
            prev_version = 0
    elif m["id"] == DEFAULT_MODEL["id"]:
        prev_version = int(DEFAULT_MODEL.get("version") or 1)
    m["version"] = max(prev_version + 1, int(m.get("version") or 1))
    m["builtin"] = False                        # a saved copy is the user's, not ours

    report = validate_model(m)
    if not report["ok"]:
        raise OntologyError("INVALID_MODEL", "The ontology model has blocking errors.",
                            detail=report)

    _write_json_atomic(path, m)
    out = json.loads(json.dumps(m))
    out["materialized"] = not is_stale(m["id"], model=m)[0]
    out["stats"] = None
    return out


def delete_model(model_id: str) -> bool:
    if model_id == "default":
        raise OntologyError("READ_ONLY", "The built-in model cannot be deleted.")
    path = _model_path(model_id)
    existed = path.exists()
    if existed:
        path.unlink()
    clear_cache(model_id)
    if active_model_id() == model_id:
        set_active_model(DEFAULT_MODEL["id"], now=utcnow_iso())
    return existed


def reset_model(model_id: str) -> dict:
    """Throw away the user's edits. For the built-in this restores the constant; for anything
    else it is a no-op that simply reloads what is on disk."""
    if model_id in ("default", DEFAULT_MODEL["id"]):
        path = _model_path(DEFAULT_MODEL["id"])
        if path.exists():
            path.unlink()
        clear_cache(DEFAULT_MODEL["id"])
        return json.loads(json.dumps(DEFAULT_MODEL))
    return load_model(model_id)


def duplicate_model(model_id: str, *, now: str) -> dict:
    src = load_model(model_id)
    base = src.get("id") or "model"
    new_id, n = f"{base}-2", 2
    while _model_path(new_id).exists():
        n += 1
        new_id = f"{base}-{n}"
    copy = json.loads(json.dumps(src))
    copy.update({"id": new_id, "name": f"{src.get('name', base)} (copy)", "builtin": False,
                 "version": 1, "created_at": now, "updated_at": now, "stats": None})
    copy.pop("materialized", None)
    _write_json_atomic(_model_path(new_id), copy)
    copy["materialized"] = False
    return copy


def active_model_id() -> str:
    try:
        return (_read_json(ACTIVE_PATH) or {}).get("active_model_id") or DEFAULT_MODEL["id"]
    except Exception:  # noqa: BLE001 — a missing/garbled pointer just means "the built-in"
        return DEFAULT_MODEL["id"]


def set_active_model(model_id: str, *, now: str) -> str:
    load_model(model_id)                        # 404s here rather than persisting a dead pointer
    _write_json_atomic(ACTIVE_PATH, {"active_model_id": model_id, "updated_at": now})
    return model_id


# ═══════════════════════════════════════════════════════════════════════════════════════
# VALIDATION
#
# validate_model() NEVER raises and NEVER discards work. It returns a report the canvas can
# use to flash the offending node or edge red, plus a live edge estimate so the builder can
# show "≈ 11 429 edges" while the user is still dragging.
# ═══════════════════════════════════════════════════════════════════════════════════════

# Under this many rows the validator projects the WHOLE corpus instead of a sample, so the
# builder's "≈ N edges" readout is exact. Above it, we sample and scale.
_FULL_SCAN_LIMIT: int = 2000


def _issue(code: str, severity: str, path: str, subject: str, message: str,
           hint: str = "", anchor: dict | None = None) -> dict:
    return {"code": code, "severity": severity, "path": path, "subject": subject,
            "anchor": anchor or {}, "message": message, "hint": hint}


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _did_you_mean(word: str, universe: Iterable[str], k: int = 3, maxd: int = 2) -> list[str]:
    scored = [(d, c) for c in universe if (d := _levenshtein(word, c)) <= maxd]
    return [c for _, c in sorted(scored)[:k]]


def validate_model(model: dict, *, dataset: Path | None = None,
                   sample: int = SAMPLE_DOCS) -> dict:
    errors: list[dict] = []
    warnings: list[dict] = []
    checked_at = utcnow_iso()
    mid = (model or {}).get("id") if isinstance(model, dict) else None

    def fail(code: str, path: str, subject: str, message: str, **kw) -> None:
        errors.append(_issue(code, "error", path, subject, message, **kw))

    def warn(code: str, path: str, subject: str, message: str, **kw) -> None:
        warnings.append(_issue(code, "warning", path, subject, message, **kw))

    if not isinstance(model, dict):
        return {"ok": False, "model_id": None, "checked_at": checked_at,
                "errors": [_issue("MODEL_NOT_OBJECT", "error", "$", "", "Model must be a JSON object.")],
                "warnings": [], "estimate": {"sampled_docs": 0, "total_docs": 0, "est_nodes": 0,
                                             "est_edges": 0, "per_relation": []}}

    if int(model.get("schema_version") or 0) != SCHEMA_VERSION:
        fail("SCHEMA_VERSION_UNSUPPORTED", "schema_version", str(model.get("schema_version")),
             f"schema_version must be {SCHEMA_VERSION}.",
             hint=f"Re-save the model from this build of the app.")
    if not ID_RE.match(str(model.get("id") or "")):
        fail("BAD_ID", "id", str(model.get("id")),
             "Model id must be lower-case letters, digits and hyphens (max 48).",
             hint="The id is also the filename, so it cannot contain slashes or dots.")
    for req in ("name", "dataset"):
        if not model.get(req):
            fail("MISSING_FIELD", req, req, f"Model is missing required field {req!r}.")
    for ts in ("created_at", "updated_at"):
        if model.get(ts) and not TS_RE.match(str(model[ts])):
            fail("BAD_TIMESTAMP", ts, str(model[ts]),
                 f"{ts} must look like 2026-08-25T14:02:11Z.")
    ds_key = model.get("dataset")
    if ds_key and ds_key not in ALLOWED_DATASETS:
        fail("UNKNOWN_DATASET", "dataset", str(ds_key),
             f"Dataset {ds_key!r} is not on the allow-list.",
             hint=f"Known datasets: {', '.join(sorted(ALLOWED_DATASETS))}.")

    ents = model.get("entity_types") or []
    rels = model.get("relation_types") or []
    if not ents:
        fail("NO_ENTITY_TYPES", "entity_types", "", "The model has no entity types.",
             hint="Drop at least the document type onto the canvas.")
    if len(ents) > MAX_ENTITY_TYPES:
        fail("TOO_MANY_ENTITY_TYPES", "entity_types", str(len(ents)),
             f"{len(ents)} entity types exceeds the cap of {MAX_ENTITY_TYPES}.")
    if len(rels) > MAX_RELATION_TYPES:
        fail("TOO_MANY_RELATION_TYPES", "relation_types", str(len(rels)),
             f"{len(rels)} relation types exceeds the cap of {MAX_RELATION_TYPES}.")

    ds_path = Path(dataset) if dataset else ALLOWED_DATASETS.get(ds_key or "", DATASET_PATH)
    docs: list[dict] = []
    total_docs = 0
    try:
        seen_ids: set[str] = set()
        dupes = 0
        for i, f in enumerate(iter_docs(ds_path)):
            total_docs += 1
            # Below the full-scan limit the "estimate" is not an estimate at all — projecting
            # 600 rows costs ~20 ms, and an exact "11 429 edges" readout in the builder is worth
            # far more than a sampled guess that lands 12% off.
            if i < max(sample, 1) or i < _FULL_SCAN_LIMIT:
                docs.append(f)
            did = str(f.get("id", ""))
            if did in seen_ids:
                dupes += 1
            seen_ids.add(did)
        if dupes:
            warn("DUPLICATE_DOC_ID", "dataset", str(dupes),
                 f"{dupes} rows share an id with an earlier row; later rows overwrite earlier ones.")
    except OntologyError as exc:
        warn("DATASET_UNREADABLE", "dataset", str(ds_path), exc.message,
             hint="Materialization will fail until the dataset is readable.")
    known_fields = set()
    for f in docs:
        known_fields.update(f.keys())

    doc_tags = [e for e in ents if ((e.get("binding") or {}).get("source")) == "doc"]
    if not doc_tags:
        fail("NO_DOC_ENTITY", "entity_types", "",
             "No entity type is bound to the document itself.",
             hint="Exactly one type must use the `row` binding — it is the join spine.")
    elif len(doc_tags) > 1:
        fail("MULTIPLE_DOC_ENTITIES", "entity_types",
             ", ".join(e.get("tag", "?") for e in doc_tags),
             "More than one entity type is bound to the document.",
             hint="Only one type can be the join spine.")
    dtag = doc_tags[0].get("tag") if doc_tags else None

    # Project every sampled row ONCE. Doing it per relation instead would re-run dish_type()
    # fourteen times per document for no new information.
    proj_rows: list[dict[str, set[str]]] = []
    for f in docs:
        row: dict[str, set[str]] = {}
        for e in ents:
            t = e.get("tag")
            if not t or ((e.get("binding") or {}).get("source")) == "doc":
                continue
            try:
                row[t] = set(_values_for(e, f))
            except OntologyError:
                row[t] = set()
        proj_rows.append(row)

    tags: set[str] = set()
    per_tag_values: dict[str, set[str]] = {}
    for i, e in enumerate(ents):
        p = f"entity_types[{i}]"
        tag = e.get("tag") or ""
        anchor = {"kind": "entity", "tag": tag}
        if not TAG_RE.match(tag):
            fail("BAD_TAG", f"{p}.tag", tag,
                 f"Entity tag {tag!r} must be lower_snake_case starting with a letter.",
                 hint="The tag becomes the node-id prefix, so it has to be terse and stable.",
                 anchor=anchor)
        if tag in tags:
            fail("DUPLICATE_TAG", f"{p}.tag", tag, f"Entity tag {tag!r} is used twice.",
                 anchor=anchor)
        tags.add(tag)
        if e.get("color") and not COLOR_RE.match(str(e["color"])):
            fail("BAD_COLOR", f"{p}.color", str(e.get("color")),
                 "Colour must be a 6-digit hex like #7c8698.", anchor=anchor)
        b = e.get("binding")
        if not isinstance(b, dict):
            fail("MISSING_BINDING", f"{p}.binding", tag,
                 f"Entity type {tag!r} has no binding.", anchor=anchor)
            continue
        src = b.get("source")
        if src not in ("doc", "field", "list_field", "derived"):
            fail("BAD_BINDING_SOURCE", f"{p}.binding.source", str(src),
                 f"Unknown binding source {src!r}.", anchor=anchor)
            continue
        if src in ("field", "list_field"):
            fname = b.get("field")
            if not fname:
                fail("BINDING_MISSING_FIELD", f"{p}.binding.field", tag,
                     f"Entity type {tag!r} binds to a field but names none.", anchor=anchor)
            elif known_fields and fname not in known_fields:
                fail("UNKNOWN_FIELD", f"{p}.binding.field", str(fname),
                     f"Field {fname!r} does not exist in the dataset.",
                     hint=f"Did you mean {', '.join(_did_you_mean(str(fname), known_fields)) or 'one of the listed fields'}?",
                     anchor=anchor)
        if src == "derived":
            if b.get("deriver") and b.get("spec"):
                fail("DERIVER_UNDERSPECIFIED", f"{p}.binding", tag,
                     "A derived binding takes exactly one of `deriver` or `spec`.", anchor=anchor)
            elif b.get("deriver"):
                if b["deriver"] not in DERIVERS:
                    fail("UNKNOWN_DERIVER", f"{p}.binding.deriver", str(b["deriver"]),
                         f"No deriver named {b['deriver']!r} is registered.",
                         hint=f"Known: {', '.join(sorted(DERIVERS))}.", anchor=anchor)
            elif isinstance(b.get("spec"), dict):
                spec = b["spec"]
                kind = spec.get("kind")
                if kind not in ("rules", "map", "bucket"):
                    fail("BAD_DERIVER_SPEC", f"{p}.binding.spec.kind", str(kind),
                         f"Deriver spec kind must be rules|map|bucket, got {kind!r}.", anchor=anchor)
                if kind == "rules":
                    for j, pair in enumerate(spec.get("rules") or []):
                        try:
                            re.compile(pair[1])
                        except (re.error, IndexError, TypeError):
                            fail("BAD_REGEX", f"{p}.binding.spec.rules[{j}]", str(pair),
                                 "Rule pattern is not a valid regular expression.", anchor=anchor)
                if kind == "bucket":
                    buckets = spec.get("buckets") or []
                    for j, bk in enumerate(buckets):
                        bound_less = "lt" not in bk and "lte" not in bk
                        if bound_less and j != len(buckets) - 1:
                            fail("BUCKET_CATCHALL_NOT_LAST", f"{p}.binding.spec.buckets[{j}]",
                                 str(bk.get("out")),
                                 "The bound-less catch-all bucket must be last, or it swallows everything.",
                                 anchor=anchor)
                    bounds = [bk.get("lt", bk.get("lte")) for bk in buckets if not
                              ("lt" not in bk and "lte" not in bk)]
                    if bounds != sorted(bounds):
                        warn("BUCKET_OVERLAP", f"{p}.binding.spec.buckets", str(spec.get("field")),
                             "Bucket bounds are not ascending; earlier buckets shadow later ones.",
                             anchor=anchor)
                    if not buckets or ("lt" in buckets[-1] or "lte" in buckets[-1]):
                        warn("BUCKET_GAP", f"{p}.binding.spec.buckets", str(spec.get("field")),
                             "No catch-all bucket: rows above the last bound produce no node.",
                             anchor=anchor)
            else:
                fail("DERIVER_UNDERSPECIFIED", f"{p}.binding", tag,
                     "A derived binding needs `deriver` or `spec`.", anchor=anchor)

        # read the projection back so the builder can warn about a type that will be nearly empty
        if docs and src != "doc":
            vals: set[str] = set()
            present = 0
            scalar_got_list = list_got_scalar = False
            for f, row in zip(docs, proj_rows):
                got = row.get(tag) or set()
                if got:
                    present += 1
                    vals.update(got)
                if src == "field" and isinstance(f.get(b.get("field")), list):
                    scalar_got_list = True
                if src == "list_field" and f.get(b.get("field")) is not None \
                        and not isinstance(f.get(b.get("field")), list):
                    list_got_scalar = True
            per_tag_values[tag] = vals
            if scalar_got_list:
                warn("LIST_ON_SCALAR_FIELD", f"{p}.binding", tag,
                     f"Field {b.get('field')!r} holds lists but is bound as a scalar; every element becomes a node.",
                     anchor=anchor)
            if list_got_scalar:
                warn("SCALAR_ON_LIST_FIELD", f"{p}.binding", tag,
                     f"Field {b.get('field')!r} holds scalars but is bound as a list; each value is wrapped in a 1-list.",
                     anchor=anchor)
            if present == 0:
                warn("EMPTY_BINDING", f"{p}.binding", tag,
                     f"Entity type {tag!r} produced no values in the first {len(docs)} rows.",
                     hint="Check the field name or the deriver.", anchor=anchor)
            elif present / len(docs) < 0.25:
                warn("SPARSE_FIELD", f"{p}.binding", tag,
                     f"Only {present} of {len(docs)} sampled rows produce a {tag!r} value.",
                     anchor=anchor)

    seen_rel: set[tuple[str, str, str]] = set()
    used_tags: set[str] = set()
    label_by_pair: dict[tuple[str, str], list[str]] = {}
    per_relation: list[dict] = []
    est_edges = 0
    for i, r in enumerate(rels):
        p = f"relation_types[{i}]"
        rel = r.get("rel") or ""
        a, b_ = r.get("from") or "", r.get("to") or ""
        anchor = {"kind": "relation", "rel": rel, "from": a, "to": b_}
        if not REL_RE.match(rel):
            fail("BAD_REL_NAME", f"{p}.rel", rel,
                 f"Relation name {rel!r} must be UPPER_SNAKE_CASE (2-40 chars).",
                 hint="Names are normalised on save — 'is served at' becomes IS_SERVED_AT.",
                 anchor=anchor)
        for side, tag in (("from", a), ("to", b_)):
            if tag not in tags:
                fail("DANGLING_TAG", f"{p}.{side}", tag,
                     f'Relation {rel} points to entity type "{tag}", which is not defined.',
                     hint=(f'Did you mean "{_did_you_mean(tag, tags)[0]}"? '
                           if _did_you_mean(tag, tags) else "") +
                          "Add the entity type, or repoint the relation.",
                     anchor=anchor)
        key = (rel, a, b_)
        if key in seen_rel:
            fail("DUPLICATE_RELATION", p, rel,
                 f"Relation {rel} from {a} to {b_} is declared twice.", anchor=anchor)
        seen_rel.add(key)
        used_tags.update({a, b_})
        label_by_pair.setdefault((a, b_), []).append(r.get("label") or rel)

        via = r.get("via") or ("doc" if dtag in (a, b_) else "cooccurrence")
        if via == "food_graph":
            warn("FOOD_GRAPH_UNSUPPORTED", f"{p}.via", rel,
                 f"Relation {rel} is declared via the food ontology, which v1 does not project.",
                 hint="The relation will be skipped at materialization.", anchor=anchor)
        if a == b_ == dtag:
            fail("SELF_LOOP_ON_DOC", p, rel,
                 f"Relation {rel} connects the document type to itself by co-occurrence — "
                 f"that is {total_docs}x{max(total_docs - 1, 0)} edges from one gesture.",
                 hint="Route it through a shared concept type instead.", anchor=anchor)
        if int(r.get("min_support") or 1) > 1 and via == "doc":
            warn("MIN_SUPPORT_NOOP", f"{p}.min_support", rel,
                 "min_support has no effect on a document-backed relation; support is always 1.",
                 anchor=anchor)

        # estimate — always produced, even when the model has errors, so the canvas can show
        # a live "≈ N edges" readout while the user is still dragging
        est = 0
        if docs and a in tags and b_ in tags and via != "food_graph":
            if via == "cooccurrence":
                # Co-occurrence collapses hard — every document contributing the same (A,B) pair
                # yields ONE edge. Counting distinct pairs is the only estimate that isn't a
                # wild over-count (IS_A: 600 documents, 31 edges).
                pairs: set[tuple[str, str]] = set()
                for row in proj_rows:
                    for va in (row.get(a) or ()):
                        for vb in (row.get(b_) or ()):
                            if va != vb or r.get("self_loops"):
                                pairs.add((va, vb))
                est = len(pairs)
            else:
                for row in proj_rows:
                    na = 1 if a == dtag else len(row.get(a) or ())
                    nb = 1 if b_ == dtag else len(row.get(b_) or ())
                    est += na * nb
                if total_docs and docs and len(docs) < total_docs:
                    est = int(round(est * total_docs / len(docs)))
        if r.get("directed") is False:
            est *= 2
        cap = int(r.get("max_edges") or MAX_EDGES_PER_RELATION)
        if est > cap:
            fail("EDGE_CAP_EXCEEDED", p, rel,
                 f"Relation {rel} would produce about {est:,} edges, over its cap of {cap:,}.",
                 hint="Raise max_edges, add min_support, or connect narrower types.", anchor=anchor)
        est_edges += est
        per_relation.append({"rel": rel, "from": a, "to": b_, "via": via, "est_edges": est})

    if est_edges > MAX_TOTAL_EDGES:
        fail("TOTAL_CAP_EXCEEDED", "relation_types", str(est_edges),
             f"The model would produce about {est_edges:,} edges, over the total cap of "
             f"{MAX_TOTAL_EDGES:,}.")

    for pair, labels in label_by_pair.items():
        if len(labels) > 1 and len(set(labels)) < len(labels):
            warn("AMBIGUOUS_REL_LABEL", "relation_types", f"{pair[0]}->{pair[1]}",
                 f"Two relations between {pair[0]} and {pair[1]} render the same edge label.",
                 hint="Give one of them a distinct label so the canvas stays readable.")

    if not rels:
        warn("NO_RELATIONS", "relation_types", "",
             "The model has no relations; every node will be isolated.",
             hint="Connect the document type to at least one concept type.")
    for e in ents:
        tag = e.get("tag")
        if tag and tag not in used_tags:
            n_vals = len(per_tag_values.get(tag, ()))
            warn("ORPHAN_TYPE", f"entity_types[{[x.get('tag') for x in ents].index(tag)}]", tag,
                 f'Entity type "{tag}" is not used by any relation; its {n_vals} nodes will be isolated.',
                 hint=f'Connect it to "{dtag}" so it is reachable when browsing.',
                 anchor={"kind": "entity", "tag": tag})

    # A cycle in the TYPE graph is a warning, never an error: materialization is one pass over
    # the corpus and never computes a transitive closure, so cycles cost exactly nothing.
    tg = nx.DiGraph()
    tg.add_nodes_from(tags)
    for r in rels:
        if r.get("from") in tags and r.get("to") in tags:
            tg.add_edge(r["from"], r["to"])
    try:
        cycle = nx.find_cycle(tg)
        warn("CYCLE_IN_TYPE_GRAPH", "relation_types",
             " -> ".join(x for x, _ in cycle) if cycle else "",
             "The type graph contains a cycle. This is legal and costs nothing to build.",
             hint="Materialization is a single pass; it never chases an edge.")
    except nx.NetworkXNoCycle:
        pass
    if dtag:
        und = tg.to_undirected()
        for tag in tags - {dtag}:
            if tag in und and dtag in und and not nx.has_path(und, dtag, tag):
                warn("UNREACHABLE_FROM_DOC", "entity_types", tag,
                     f'Entity type "{tag}" cannot be reached from "{dtag}"; the query bar will '
                     f"not offer it as a slot.",
                     anchor={"kind": "entity", "tag": tag})

    est_nodes = total_docs + sum(len(v) for v in per_tag_values.values())
    return {
        "ok": len(errors) == 0,
        "model_id": mid,
        "checked_at": checked_at,
        "errors": errors,
        "warnings": warnings,
        "estimate": {"sampled_docs": len(docs), "total_docs": total_docs,
                     "est_nodes": est_nodes, "est_edges": est_edges,
                     "per_relation": per_relation},
    }


# ═══════════════════════════════════════════════════════════════════════════════════════
# MATERIALIZATION — TBox + corpus -> ABox
#
# Cost is O(D · Σ_r |A_r|·|B_r|): one pass over the documents, no traversal, no closure. That
# is why a cycle in the type graph is free and why 600 dishes compile in ~11 ms.
# ═══════════════════════════════════════════════════════════════════════════════════════

def _values_for(etype: dict, fields: dict) -> list[str]:
    """Raw cell(s) -> normalized node NAMEs for one entity type on one document.

    Permissive on shape, loud in the validator: a list on a scalar binding is unrolled rather
    than dropped, because losing a user's data to a type quibble is the worse failure.
    """
    b = etype.get("binding") or {}
    src = b.get("source")
    spec = etype.get("normalize") or NORM_DEFAULT
    drop = set(etype.get("drop_values") or DROP_VALUES)

    if src == "field" or src == "list_field":
        raw = fields.get(b.get("field"))
        vals = raw if isinstance(raw, list) else ([] if raw in (None, "") else [raw])
    elif src == "derived":
        vals = derive(resolve_deriver(b), fields)
    else:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for v in vals:
        n = _norm(v, spec)
        if not n or n in drop:
            continue
        # dedupe is PER DOCUMENT, which is what makes the node's `n` a true document frequency
        if spec.get("dedupe", True):
            if n in seen:
                continue
            seen.add(n)
        out.append(n)
    return out


def _raw_label(etype: dict, fields: dict, normalized: str) -> str:
    """The first-seen raw spelling for a normalized value, so the canvas shows 'Italian'."""
    b = etype.get("binding") or {}
    if b.get("source") in ("field", "list_field"):
        raw = fields.get(b.get("field"))
        vals = raw if isinstance(raw, list) else [raw]
        for v in vals:
            if _norm(v, etype.get("normalize") or NORM_DEFAULT) == normalized:
                return str(v)
    return normalized


def _payload_for(etype: dict, fields: dict, proj_dish_type: str | None) -> dict:
    b = etype.get("binding") or {}
    out: dict = {}
    for f in b.get("payload_fields") or []:
        if f not in fields or fields[f] is None:
            continue                                # omit rather than emit null
        key = PAYLOAD_ALIASES.get(f, f)
        val = fields[f]
        if key == "desc" and isinstance(val, str) and len(val) > PAYLOAD_DESC_LEN:
            val = val[:PAYLOAD_DESC_LEN].rstrip() + "…"
        out[key] = val                              # RAW: the card wants "Italian", not "italian"
    if proj_dish_type:
        # The photo key is the FOOD_RULES category from App.jsx, not the new dish_type leaf —
        # it addresses web/public/food/{img}.jpg, and those filenames are not ours to rename.
        out["img"] = PHOTO_KEY.get(proj_dish_type, "platter")
    return out


def materialize(model: dict, *, dataset: Path = DATASET_PATH, now: str = "",
                persist: bool = True) -> tuple[nx.DiGraph, dict]:
    """Compile a model into a graph. Always validates first — there is no unchecked path.

    `now == ""` is legal and means "don't stamp built_at", which makes the output
    byte-reproducible in a test.
    """
    t_val = time.perf_counter()
    report = validate_model(model, dataset=dataset)
    if not report["ok"]:
        raise OntologyError("INVALID_MODEL", "Cannot materialize a model with blocking errors.",
                            detail=report)
    validate_ms = (time.perf_counter() - t_val) * 1000.0
    t0 = time.perf_counter()   # elapsed_ms covers phases 1-7 only; validation is reported apart

    ds_path = Path(dataset) if dataset != DATASET_PATH else \
        ALLOWED_DATASETS.get(model.get("dataset") or "", DATASET_PATH)
    ents = model.get("entity_types") or []
    rels = model.get("relation_types") or []
    by_tag = {e.get("tag"): e for e in ents}
    dtype = _doc_type(model)
    dtag = dtype.get("tag") if dtype else None
    has_dish_type = any(e.get("tag") == "dish_type" for e in ents)

    g = nx.DiGraph()
    doc_rows: list[tuple[str, dict[str, list[str]]]] = []
    docs_read = docs_skipped = 0

    # ---- phase 1-3: one pass over the corpus, creating nodes and buffering the projection ----
    for fields in iter_docs(ds_path):
        docs_read += 1
        proj: dict[str, list[str]] = {}
        for e in ents:
            tag = e.get("tag")
            if tag == dtag:
                continue
            proj[tag] = _values_for(e, fields)

        doc_node = None
        if dtag:
            b = dtype.get("binding") or {}
            raw_id = fields.get(b.get("id_field") or "id")
            if raw_id in (None, ""):
                docs_skipped += 1
                continue
            # the id field is used VERBATIM — lower-casing an identifier is a collision bug
            doc_node = f"{dtag}:{raw_id}"
            label = str(fields.get(b.get("label_field") or "name") or raw_id)
            leaf = (proj.get("dish_type") or [None])[0] if has_dish_type else None
            g.add_node(doc_node, kind=dtag, name=_norm(label), label=label, n=1,
                       payload=_payload_for(dtype, fields, leaf))

        for tag, vals in proj.items():
            e = by_tag[tag]
            for v in vals:
                nid = f"{tag}:{v}"
                if nid in g:
                    g.nodes[nid]["n"] += 1
                else:
                    raw = _raw_label(e, fields, v)
                    attrs = {"kind": tag, "name": v, "label": raw, "n": 1}
                    if tag == "cuisine" and raw in SURROGATE_CUISINES:
                        # Breakfast is a meal_type, Salads & Bowls a dish family. Flag, don't
                        # rewrite: /api/search and the Vespa dish schema key off these strings.
                        attrs["surrogate"] = True
                    g.add_node(nid, **attrs)
        if doc_node:
            doc_rows.append((doc_node, proj))

    # ---- phase 4-6: relations ----
    rel_stats: list[dict] = []
    for r in rels:
        rel = r.get("rel")
        a, b_ = r.get("from"), r.get("to")
        via = r.get("via") or ("doc" if dtag in (a, b_) else "cooccurrence")
        directed = r.get("directed", True) is not False
        min_support = max(int(r.get("min_support") or 1), 1)
        cap = int(r.get("max_edges") or MAX_EDGES_PER_RELATION)
        self_loops = bool(r.get("self_loops"))
        if via == "food_graph":
            # declared in the schema, not projected in v1 — the validator already said so
            rel_stats.append({"rel": rel, "label": r.get("label") or rel, "from": a, "to": b_,
                              "via": via, "edges": 0, "pruned": 0, "capped": False,
                              "max_support": 0, "skipped": "food_graph unsupported in v1"})
            continue

        pairs: dict[tuple[str, str], int] = {}
        for doc_node, proj in doc_rows:
            srcs = [doc_node] if a == dtag else [f"{a}:{v}" for v in proj.get(a, ())]
            dsts = [doc_node] if b_ == dtag else [f"{b_}:{v}" for v in proj.get(b_, ())]
            for s in srcs:
                for t in dsts:
                    if s == t and not self_loops:
                        continue
                    pairs[(s, t)] = pairs.get((s, t), 0) + 1

        pruned = sum(1 for k, v in pairs.items() if v < min_support)
        kept = [(k, v) for k, v in pairs.items() if v >= min_support]
        capped = len(kept) > cap
        if capped:
            kept.sort(key=lambda kv: (-kv[1], kv[0]))   # keep the best-supported edges
            kept = kept[:cap]

        added = 0
        max_support = 0
        for (s, t), sup in kept:
            max_support = max(max_support, sup)
            for u, v in ((s, t),) if directed else ((s, t), (t, s)):
                if g.has_edge(u, v):
                    # nx.DiGraph holds ONE edge per ordered pair. Switching to MultiDiGraph
                    # would break ingest/graph.py's renderers and the `from->to` edge ids the
                    # frontend uses, so the extras ride along as an attribute instead.
                    also = g.edges[u, v].setdefault("also_rels", [])
                    if rel not in also and g.edges[u, v].get("rel") != rel:
                        also.append(rel)
                    continue
                g.add_edge(u, v, rel=rel, label=r.get("label") or rel, via=via, support=sup)
                added += 1

        rel_stats.append({"rel": rel, "label": r.get("label") or rel, "from": a, "to": b_,
                          "via": via, "edges": added, "pruned": pruned, "capped": capped,
                          "max_support": max_support})

    # ---- phase 7: stats ----
    ent_stats: list[dict] = []
    for e in ents:
        tag = e.get("tag")
        ids = [n for n, d in g.nodes(data=True) if d.get("kind") == tag]
        top = [] if tag == dtag else sorted(
            ({"value": n.split(":", 1)[1], "label": g.nodes[n].get("label"), "n": g.nodes[n]["n"]}
             for n in ids), key=lambda x: (-x["n"], x["value"]))[:TOP_VALUES]
        # Is this type WORTH having? Node counts alone cannot say: a 600-dish corpus projected
        # onto 230 name-nodes and onto 8 diet-nodes both "built fine". The two numbers that
        # separate a useful facet from a modelling mistake are how many distinct values it has
        # per document (a near-identifier groups nothing) and how big its single largest value
        # is (a value covering ~everything filters nothing).
        quality: dict = {}
        if tag != dtag and ids and docs_read:
            members = [g.nodes[n]["n"] for n in ids]
            distinct = len(ids)
            top_n = max(members)
            quality = {
                "distinct": distinct,
                "docs": docs_read,
                "values_per_doc": round(distinct / docs_read, 3),
                "mean_members": round(sum(members) / distinct, 1),
                "top_share": round(top_n / docs_read, 3),
            }
        ent_stats.append({"tag": tag, "label": e.get("label") or tag,
                          "plural": e.get("plural") or (e.get("label") or tag) + "s",
                          "color": e.get("color") or "#9aa0a6",
                          "source": (e.get("binding") or {}).get("source"),
                          "nodes": len(ids), "top": top, "quality": quality})

    quality_warnings: list[dict] = []
    for es in ent_stats:
        q = es.get("quality") or {}
        if not q:
            continue
        lbl = es.get("label") or es["tag"]
        # mean_members is the honest test, not values_per_doc: 230 name-nodes over 600 dishes is
        # only 0.38 values/doc yet averages 2.6 dishes each -- it groups nothing. A facet has to
        # put a useful number of documents behind each value to be worth a slot.
        if q["distinct"] > 20 and (q["values_per_doc"] > 0.5 or q["mean_members"] < 3.0):
            quality_warnings.append({
                "code": "HIGH_CARDINALITY_TYPE", "severity": "warning",
                "path": f'entity_types[{es["tag"]}]', "subject": es["tag"],
                "anchor": {"kind": "entity", "tag": es["tag"]},
                "message": (f'"{lbl}" has {q["distinct"]} distinct values across '
                            f'{q["docs"]} documents ({q["mean_members"]} each) — it behaves like '
                            f"an identifier, not a category, so it will not group anything."),
                "hint": "Bucket it, or bind it as a payload field on the document instead.",
            })
        if q["top_share"] > 0.85:
            top1 = (es.get("top") or [{}])[0]
            quality_warnings.append({
                "code": "LOW_DISCRIMINATION_TYPE", "severity": "warning",
                "path": f'entity_types[{es["tag"]}]', "subject": es["tag"],
                "anchor": {"kind": "entity", "tag": es["tag"]},
                "message": (f'"{lbl}" is dominated by {top1.get("label") or top1.get("value")!r}, '
                            f'which covers {int(q["top_share"] * 100)}% of documents — filtering '
                            f"on it removes almost nothing."),
                "hint": "Useful as a reassurance badge, weak as a query slot.",
            })

    orphans = [n for n in g.nodes if g.degree(n) == 0]
    elapsed = (time.perf_counter() - t0) * 1000.0
    stats = {
        "model_id": model.get("id"), "model_name": model.get("name"),
        "model_version": int(model.get("version") or 1),
        "built_at": now or None, "elapsed_ms": round(elapsed, 1), "persist_ms": 0.0,
        "validate_ms": round(validate_ms, 1),
        "builder_version": BUILDER_VERSION,
        "model_hash": model_hash(model), "dataset_hash": dataset_hash(ds_path),
        "dataset": model.get("dataset") or "data/dishes.jsonl",
        "docs_read": docs_read, "docs_skipped": docs_skipped,
        "nodes": g.number_of_nodes(), "edges": g.number_of_edges(),
        "orphans": len(orphans), "orphan_nodes": sorted(orphans)[:MAX_ORPHANS_LISTED],
        "entity_counts": sorted(ent_stats, key=lambda x: -x["nodes"]),
        "relation_counts": sorted(rel_stats, key=lambda x: -x["edges"]),
        "warnings": list(report["warnings"]) + quality_warnings,
    }

    if persist:
        t1 = time.perf_counter()
        _persist_graph(model, g, stats)
        stats["persist_ms"] = round((time.perf_counter() - t1) * 1000.0, 1)
    return g, stats


# ---- persistence ---------------------------------------------------------------------

def _graph_path(model_id: str) -> Path:
    if not ID_RE.match(model_id or ""):
        raise OntologyError("BAD_ID", f"Invalid model id {model_id!r}.", detail={"id": model_id})
    return GRAPHS_DIR / f"{model_id}.graph.json"


def _meta_path(model_id: str) -> Path:
    if not ID_RE.match(model_id or ""):
        raise OntologyError("BAD_ID", f"Invalid model id {model_id!r}.", detail={"id": model_id})
    return GRAPHS_DIR / f"{model_id}.meta.json"


def _persist_graph(model: dict, g: nx.DiGraph, stats: dict) -> None:
    mid = model.get("id")
    # GRAPH FIRST, then meta. A crash between the two leaves is_stale() saying "never built",
    # which is safe; the reverse order would advertise a build that does not exist.
    _write_json_atomic(_graph_path(mid), nx.node_link_data(g, edges="edges"))
    _write_json_atomic(_meta_path(mid), {
        "model_id": mid, "model_hash": stats["model_hash"], "dataset_hash": stats["dataset_hash"],
        "builder_version": BUILDER_VERSION, "built_at": stats.get("built_at"),
        "stats": stats,
    })


def _read_meta(model_id: str) -> dict | None:
    try:
        return _read_json(_meta_path(model_id))
    except Exception:  # noqa: BLE001
        return None


def is_stale(model_id: str, *, model: dict | None = None) -> tuple[bool, str]:
    """Three hashes, checked in order, first mismatch wins. The reason string is shown verbatim
    in the UI, so it is written for a person, not for a log."""
    try:
        gp = _graph_path(model_id)
    except OntologyError:
        return True, "the model id is not valid"
    if not gp.exists():
        return True, "never built"
    meta = _read_meta(model_id)
    if not meta:
        return True, "never built"
    m = model
    if m is None:
        try:
            m = load_model(model_id)
        except OntologyError:
            return True, "the ontology model is missing"
    if meta.get("model_hash") != model_hash(m):
        return True, "the ontology model changed"
    ds = ALLOWED_DATASETS.get(m.get("dataset") or "", DATASET_PATH)
    if meta.get("dataset_hash") != dataset_hash(ds):
        return True, "the dish data changed"
    if int(meta.get("builder_version") or 0) != BUILDER_VERSION:
        return True, "the materializer was updated"
    return False, ""


def load_materialized(model_id: str, *, rebuild_if_stale: bool = True, now: str = "",
                      model: dict | None = None) -> tuple[nx.DiGraph | None, dict]:
    # `model` lets a caller that already holds the document (a save-then-build round trip, or
    # an unsaved draft being previewed) skip the disk read entirely.
    model = model if model is not None else load_model(model_id)
    stale, reason = is_stale(model_id, model=model)
    if stale:
        if not rebuild_if_stale:
            return None, {"stale": True, "stale_reason": reason, "model_id": model_id}
        g, stats = materialize(model, now=now or utcnow_iso())
        return g, {"stale": False, "stale_reason": "", "rebuilt": True, **stats}
    try:
        data = _read_json(_graph_path(model_id))
        g = nx.node_link_graph(data, directed=True, multigraph=False, edges="edges")
    except Exception:  # noqa: BLE001
        # A corrupt cache is never a 500 — delete it and rebuild. The graph is derived data;
        # nothing is lost by throwing it away.
        try:
            _graph_path(model_id).unlink(missing_ok=True)
            _meta_path(model_id).unlink(missing_ok=True)
        except OSError:
            pass
        if not rebuild_if_stale:
            return None, {"stale": True, "stale_reason": "the saved graph is unreadable",
                          "model_id": model_id}
        g, stats = materialize(model, now=now or utcnow_iso())
        return g, {"stale": False, "stale_reason": "", "rebuilt": True, **stats}
    meta = _read_meta(model_id) or {}
    return g, {"stale": False, "stale_reason": "", "rebuilt": False, **(meta.get("stats") or {})}


def clear_cache(model_id: str | None = None) -> int:
    n = 0
    if not GRAPHS_DIR.exists():
        return 0
    pattern = f"{model_id}.*" if model_id else "*"
    for p in GRAPHS_DIR.glob(pattern):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    _INDEX_CACHE.pop(model_id, None) if model_id else _INDEX_CACHE.clear()
    return n


def graph_stats(g: nx.DiGraph, *, model: dict | None = None) -> dict:
    by_kind: dict[str, int] = {}
    for _, d in g.nodes(data=True):
        by_kind[d.get("kind", "?")] = by_kind.get(d.get("kind", "?"), 0) + 1
    out = {"nodes": g.number_of_nodes(), "edges": g.number_of_edges(),
           "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1]))}
    if model:
        out["model_id"] = model.get("id")
        out["model_name"] = model.get("name")
    return out


# ═══════════════════════════════════════════════════════════════════════════════════════
# READ PRIMITIVES — graph MECHANICS, deliberately free of query semantics
# ═══════════════════════════════════════════════════════════════════════════════════════

def nodes_by_tag(g: nx.DiGraph, tag: str) -> list[str]:
    return [n for n, d in g.nodes(data=True) if d.get("kind") == tag]


def vocab(g: nx.DiGraph, tag: str, prefix: str = "", limit: int = 20) -> list[dict]:
    p = _vkey(prefix)
    rows = []
    for n in nodes_by_tag(g, tag):
        name = g.nodes[n].get("name", "")
        if p and not (name.startswith(p) or p in name):
            continue
        rows.append({"value": g.nodes[n].get("label") or name, "label": g.nodes[n].get("label") or name,
                     "node": n, "count": g.nodes[n].get("n", 0),
                     "match": "prefix" if name.startswith(p) else "substring"})
    rows.sort(key=lambda r: (0 if r["match"] == "prefix" else 1, -r["count"], r["label"]))
    return rows[:limit]


def _gnode(g: nx.DiGraph, nid: str) -> dict:
    d = g.nodes[nid]
    out = {"id": nid, "kind": d.get("kind"), "label": d.get("label") or d.get("name"),
           "n": d.get("n", 1)}
    if d.get("surrogate"):
        out["surrogate"] = True
    return out


def neighbors(g: nx.DiGraph, node: str, *, limit: int = 25) -> dict:
    """Click-to-expand. Response shape is byte-identical to /api/graph/neighbors so one
    renderer serves both the food ontology and the model graph."""
    if node not in g:
        return {"nodes": [], "edges": [], "truncated": False, "total": 0}
    out_e = list(g.out_edges(node, data=True))
    in_e = list(g.in_edges(node, data=True))
    total = len(out_e) + len(in_e)
    nodes = {node: _gnode(g, node)}
    edges = []
    for _, t, d in out_e[:limit]:
        nodes[t] = _gnode(g, t)
        edges.append({"from": node, "to": t, "rel": d.get("rel"), "label": d.get("label")})
    for s, _, d in in_e[:limit]:
        nodes[s] = _gnode(g, s)
        edges.append({"from": s, "to": node, "rel": d.get("rel"), "label": d.get("label")})
    return {"nodes": list(nodes.values()), "edges": edges,
            "truncated": total > len(edges), "total": total}


def roots(g: nx.DiGraph, model: dict | None = None, limit: int = 40) -> list[dict]:
    """Entry points for the explorer: the busiest node of each non-doc tag, then the next
    busiest overall. NEVER 600 dish nodes — an explorer that opens on the document type is
    unreadable and stalls the physics engine."""
    dtag = doc_tag(model) if model else "dish"
    by_kind: dict[str, list[str]] = {}
    for n, d in g.nodes(data=True):
        k = d.get("kind")
        if k == dtag:
            continue
        by_kind.setdefault(k, []).append(n)
    picked: list[str] = []
    for k, ids in by_kind.items():
        ids.sort(key=lambda n: (-g.nodes[n].get("n", 0), n))
        picked.append(ids[0])
    rest = sorted((n for ids in by_kind.values() for n in ids[1:]),
                  key=lambda n: (-g.nodes[n].get("n", 0), n))
    picked.extend(rest[:max(limit - len(picked), 0)][:8])
    return [_gnode(g, n) for n in picked[:limit]]


def search_nodes(g: nx.DiGraph, q: str, limit: int = 12) -> list[dict]:
    t = _vkey(q)
    if not t:
        return []
    hits = []
    for n, d in g.nodes(data=True):
        name = d.get("name", "")
        if name.startswith(t):
            hits.append((0, -d.get("n", 0), n))
        elif t in name:
            hits.append((1, -d.get("n", 0), n))
    hits.sort()
    return [_gnode(g, n) for _, _, n in hits[:limit]]


def docs_for(g: nx.DiGraph, node: str, *, doc_tag: str = "dish") -> set[str]:
    """The documents attached to a concept node. This one line is the entire query engine:
    intersecting two of these answers `diet:vegan cuisine:Italian` in ~0.1 ms."""
    if node not in g:
        return set()
    out = {s for s, _ in g.in_edges(node) if g.nodes[s].get("kind") == doc_tag}
    out |= {t for _, t in g.out_edges(node) if g.nodes[t].get("kind") == doc_tag}
    return out


def payload_of(g: nx.DiGraph, node: str) -> dict:
    return dict((g.nodes[node].get("payload") or {})) if node in g else {}


def to_vis(g: nx.DiGraph, node_ids: list[str], model: dict) -> dict:
    """Node/edge lists shaped for vis-network, coloured from the model's own palette."""
    colors = {e.get("tag"): e.get("color") for e in (model or {}).get("entity_types") or []}
    rel_color = {r.get("rel"): r.get("color") for r in (model or {}).get("relation_types") or []}
    keep = [n for n in node_ids if n in g]
    kset = set(keep)
    nodes = []
    for n in keep:
        d = g.nodes[n]
        nodes.append({"id": n, "label": d.get("label") or d.get("name"), "kind": d.get("kind"),
                      "color": colors.get(d.get("kind"), "#9aa0a6"), "value": d.get("n", 1),
                      "title": f"{d.get('kind')}: {d.get('label')} ({d.get('n', 1)})"})
    edges = []
    for u, v, d in g.edges(data=True):
        if u in kset and v in kset:
            edges.append({"id": f"{u}->{v}", "from": u, "to": v, "rel": d.get("rel"),
                          "label": d.get("label") or d.get("rel"),
                          "color": rel_color.get(d.get("rel"), "#98a2b3")})
    return {"nodes": nodes, "edges": edges}


# ═══════════════════════════════════════════════════════════════════════════════════════
# THE QUERY LAYER
#
# An inverted index over the materialized graph, a small DSL, and set algebra. Complexity is
# O(k·N + |R| log |R|) with NO graph traversal at query time — measured 0.4-1.8 ms on 600
# subjects. Nothing below hardcodes a relation name: postings are read off the EDGES, so
# renaming HAS_CUISINE to WIBBLE in the model changes nothing about what a query returns.
# That property is the whole claim that this feature is driven by the user's ontology.
# ═══════════════════════════════════════════════════════════════════════════════════════

# tag -> the payload key holding its numeric value. Measures are predicates on the document
# node, never nodes of their own: a node per distinct price would be 600 useless nodes.
MEASURE_ATTRS: dict[str, str] = {
    "price_pp": "price_pp", "serves": "serves", "popularity": "popularity",
    "spice": "spice_level", "price": "price",
}
MEASURE_META: dict[str, dict] = {
    "price_pp":   {"label": "Price / head", "unit": "$", "default_op": "lte", "order": 80},
    "price":      {"label": "Tray price", "unit": "$", "default_op": "lte", "order": 84},
    "serves":     {"label": "Serves", "unit": "people", "default_op": "gte", "order": 82},
    "spice":      {"label": "Spice level", "unit": "", "default_op": "gte", "order": 86},
    "popularity": {"label": "Popularity", "unit": "", "default_op": "gte", "order": 88},
}

ALIASES: dict[str, str] = {
    "dietary": "diet", "dietary_capabilities": "diet", "dietary_capability": "diet",
    "diets": "diet",
    "allergens": "allergen", "allergy": "allergen",
    "ingredients": "ingredient",
    "type": "dish_type", "dish": "dish_type",
    "family": "dish_type_group", "group": "dish_type_group",
    "meal": "meal_type",
    "price": "price_pp", "budget": "price_pp", "pp": "price_pp",
    "spice_level": "spice", "heat": "spice",
    "headcount": "serves", "people": "serves", "guests": "serves",
    "caterer_name": "caterer", "vendor": "caterer",
}

_WORD_RE = re.compile(r"[a-z0-9']+")


@dataclass(frozen=True)
class Slot:
    """A slot DESCRIPTOR — what CAN be filled, not a filled slot. The query bar's chip palette
    and its typeahead are both generated from these, which is why adding an entity type to the
    canvas immediately adds a slot to the bar."""
    tag: str
    label: str
    plural: str = ""
    kind: str = "entity"                    # entity | measure | text
    cardinality: str = "many"
    polarity: str = "both"
    default_polarity: str = "include"
    safety_critical: bool = False
    relation: dict | None = None
    value_count: int = 0
    coverage: float = 0.0
    examples: tuple[str, ...] = ()
    color: str = "#9aa0a6"
    icon: str = ""
    aliases: tuple[str, ...] = ()
    order: int = 50
    attr: str = ""
    unit: str = ""
    min: float = 0.0
    max: float = 0.0
    median: float = 0.0
    ops: tuple[str, ...] = ()
    default_op: str = ""
    fields: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        d = {"tag": self.tag, "label": self.label, "kind": self.kind,
             "polarity": self.polarity, "order": self.order,
             "aliases": list(self.aliases)}
        if self.kind == "entity":
            d.update({"plural": self.plural, "cardinality": self.cardinality,
                      "default_polarity": self.default_polarity,
                      "safety_critical": self.safety_critical, "relation": self.relation,
                      "value_count": self.value_count, "coverage": round(self.coverage, 4),
                      "examples": list(self.examples), "color": self.color, "icon": self.icon})
        elif self.kind == "measure":
            d.update({"attr": self.attr, "unit": self.unit, "min": self.min, "max": self.max,
                      "median": self.median, "ops": list(self.ops),
                      "default_op": self.default_op})
        else:
            d.update({"fields": list(self.fields)})
        return d


@dataclass
class KGIndex:
    version: str
    model_id: str
    subject: str
    universe: frozenset
    postings: dict
    values: dict
    value_to_tags: dict
    subject_values: dict
    measures: dict
    text: dict
    tokens: dict
    doc_len: dict
    attrs: dict
    base: dict
    ta_rows: list
    slots: dict
    model: dict
    g: Any = None
    idf: dict = _dc_field(default_factory=dict)
    avg_len: float = 1.0


_INDEX_CACHE: dict[str, KGIndex] = {}


def index_version(model: dict) -> str:
    raw = f"{model_hash(model)}|{dataset_hash(ALLOWED_DATASETS.get(model.get('dataset') or '', DATASET_PATH))}|{BUILDER_VERSION}"
    return "kgi-" + hashlib.sha1(raw.encode()).hexdigest()[:8]


def build_index(g: nx.DiGraph, model: dict) -> KGIndex:
    subject = doc_tag(model)
    universe = frozenset(n for n, d in g.nodes(data=True) if d.get("kind") == subject)

    postings: dict[str, dict[str, set]] = {}
    values: dict[str, dict[str, dict]] = {}
    subject_values: dict[str, dict[str, set]] = {s: {} for s in universe}
    rel_hint: dict[str, dict[tuple[str, str], int]] = {}

    # Postings come off the EDGES, never off a relation-name lookup. That is what makes the
    # query engine independent of what the user called their relations.
    for sid in universe:
        sv = subject_values[sid]
        for _, t, ed in g.out_edges(sid, data=True):
            _absorb(g, postings, values, sv, rel_hint, sid, t, ed, "out", subject)
        for s, _, ed in g.in_edges(sid, data=True):
            _absorb(g, postings, values, sv, rel_hint, sid, s, ed, "in", subject)

    frozen_postings = {t: {v: frozenset(ids) for v, ids in vs.items()} for t, vs in postings.items()}
    frozen_sv = {s: {t: frozenset(vs) for t, vs in d.items()} for s, d in subject_values.items()}

    value_to_tags: dict[str, tuple[str, ...]] = {}
    for t, vs in frozen_postings.items():
        for v in vs:
            value_to_tags[v] = value_to_tags.get(v, ()) + (t,)

    measures: dict[str, dict[str, float]] = {}
    attrs: dict[str, dict] = {}
    text: dict[str, str] = {}
    tokens: dict[str, frozenset] = {}
    doc_len: dict[str, int] = {}
    base: dict[str, str] = {}
    df: dict[str, int] = {}
    for sid in universe:
        pl = g.nodes[sid].get("payload") or {}
        attrs[sid] = pl
        for tag, key in MEASURE_ATTRS.items():
            if key in pl:
                try:
                    measures.setdefault(tag, {})[sid] = float(pl[key])
                except (TypeError, ValueError):
                    pass
        blob = f"{g.nodes[sid].get('label') or ''} {pl.get('desc') or ''}".lower()
        text[sid] = blob
        tk = frozenset(_WORD_RE.findall(blob))
        tokens[sid] = tk
        doc_len[sid] = max(len(_WORD_RE.findall(blob)), 1)
        base[sid] = base_name(g.nodes[sid].get("label") or sid)
        for w in tk:
            df[w] = df.get(w, 0) + 1

    n_docs = max(len(universe), 1)
    idf = {w: math.log(1 + (n_docs - c + 0.5) / (c + 0.5)) for w, c in df.items()}
    avg_len = sum(doc_len.values()) / n_docs if doc_len else 1.0

    ta_rows = sorted(
        ((v, t, values[t][v]["label"], len(frozen_postings[t][v]))
         for t in frozen_postings for v in frozen_postings[t]),
        key=lambda r: (-r[3], r[0]))

    idx = KGIndex(version=index_version(model), model_id=model.get("id", ""), subject=subject,
                  universe=universe, postings=frozen_postings, values=values,
                  value_to_tags=value_to_tags, subject_values=frozen_sv, measures=measures,
                  text=text, tokens=tokens, doc_len=doc_len, attrs=attrs, base=base,
                  ta_rows=ta_rows, slots={}, model=model, g=g, idf=idf, avg_len=avg_len)
    idx.slots = build_slots(model, g, idx, rel_hint)
    return idx


def _absorb(g, postings, values, sv, rel_hint, sid, other, ed, direction, subject) -> None:
    d = g.nodes[other]
    tag = d.get("kind")
    if not tag or tag == subject:
        return
    v = d.get("name", "")
    postings.setdefault(tag, {}).setdefault(v, set()).add(sid)
    values.setdefault(tag, {}).setdefault(v, {"label": d.get("label") or v, "node": other,
                                             "count": d.get("n", 0)})
    sv.setdefault(tag, set()).add(v)
    key = (ed.get("rel") or "", direction)
    rel_hint.setdefault(tag, {})[key] = rel_hint.setdefault(tag, {}).get(key, 0) + 1


def build_slots(model: dict, g: nx.DiGraph, index: KGIndex,
                rel_hint: dict | None = None) -> dict[str, Slot]:
    rel_hint = rel_hint or {}
    by_tag = {e.get("tag"): e for e in model.get("entity_types") or []}
    rel_labels = {r.get("rel"): r for r in model.get("relation_types") or []}
    n = max(len(index.universe), 1)
    out: dict[str, Slot] = {}

    for tag, posts in index.postings.items():
        e = by_tag.get(tag, {})
        hits = rel_hint.get(tag, {})
        rel_info = None
        if hits:
            (rname, direction), _ = max(hits.items(), key=lambda kv: (kv[1], kv[0]))
            rmeta = rel_labels.get(rname, {})
            alternates = sorted({r for (r, _) in hits if r != rname})
            rel_info = {"name": rname, "direction": direction,
                        "label": rmeta.get("label") or rname, "alternates": alternates}
        covered = len({s for ids in posts.values() for s in ids})
        top = sorted(posts.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:3]
        safety = tag in SAFETY_CRITICAL_TAGS
        aliases = tuple(sorted(a for a, t in ALIASES.items() if t == tag))
        out[tag] = Slot(
            tag=tag, label=e.get("label") or tag.replace("_", " ").title(),
            plural=e.get("plural") or (e.get("label") or tag) + "s",
            kind="entity",
            cardinality="one" if all(len(v) == 1 for v in index.subject_values.values()
                                     if tag in v) else "many",
            polarity="both", default_polarity="exclude" if safety else "include",
            safety_critical=safety, relation=rel_info,
            value_count=len(posts), coverage=covered / n,
            examples=tuple(index.values[tag][v]["label"] for v, _ in top),
            color=e.get("color") or "#9aa0a6", icon=e.get("icon") or "",
            aliases=aliases, order=0)

    for tag, series in index.measures.items():
        if not series:
            continue
        meta = MEASURE_META.get(tag, {"label": tag, "unit": "", "default_op": "lte", "order": 90})
        vals = sorted(series.values())
        out[tag] = Slot(tag=tag, label=meta["label"], kind="measure", polarity="measure",
                        attr=MEASURE_ATTRS.get(tag, tag), unit=meta["unit"],
                        min=round(vals[0], 2), max=round(vals[-1], 2),
                        median=round(vals[len(vals) // 2], 2),
                        ops=("<", "<=", "=", ">=", ">"), default_op=meta["default_op"],
                        aliases=tuple(sorted(a for a, t in ALIASES.items() if t == tag)),
                        order=meta["order"])

    out["text"] = Slot(tag="text", label="Free text", kind="text", polarity="both",
                       fields=("label", "description"), order=99)

    # Safety-critical types are forced into the first five; everything else sorts by how much
    # of the corpus it covers. The order is STABLE so the chip palette never reshuffles.
    entities = [s for s in out.values() if s.kind == "entity"]
    entities.sort(key=lambda s: (0 if s.safety_critical else 1, -s.coverage, s.tag))
    for i, s in enumerate(entities):
        out[s.tag] = Slot(**{**s.__dict__, "order": (i + 1) * 10 if i >= 5 else (i + 1) * 2})
    return out


def get_index(model_id: str) -> KGIndex:
    """Cached per process, revalidated on every call. Rebuilding is ~40 ms and the correctness
    cost of serving a stale index is a wrong answer, so 'when in doubt, rebuild' is right."""
    model = load_model(model_id)
    want = index_version(model)
    cached = _INDEX_CACHE.get(model_id)
    if cached is not None and cached.version == want:
        return cached
    g, _meta = load_materialized(model_id, rebuild_if_stale=True, now=utcnow_iso())
    if g is None:
        raise OntologyError("NOT_FOUND", f"Model {model_id!r} has no materialized graph.",
                            detail={"id": model_id})
    idx = build_index(g, model)
    _INDEX_CACHE[model_id] = idx
    return idx


def infer_model_from_graph(g: nx.DiGraph, subject: str = "dish") -> dict:
    """Reconstruct a serviceable model from a bare graph. The fallback that keeps the query bar
    alive when the model file is deleted but the ABox survives."""
    kinds: dict[str, int] = {}
    for _, d in g.nodes(data=True):
        kinds[d.get("kind", "?")] = kinds.get(d.get("kind", "?"), 0) + 1
    ents = [{"tag": subject, "label": subject.title(),
             "binding": {"source": "doc", "id_field": "id", "label_field": "name"},
             "color": "#e35205"}]
    ents += [{"tag": k, "label": k.replace("_", " ").title(),
              "binding": {"source": "field", "field": k}, "color": "#9aa0a6"}
             for k in sorted(kinds) if k != subject and k != "?"]
    rels: dict[tuple, dict] = {}
    for u, v, d in g.edges(data=True):
        key = (d.get("rel"), g.nodes[u].get("kind"), g.nodes[v].get("kind"))
        if key[0] and key not in rels:
            rels[key] = {"rel": key[0], "label": d.get("label") or key[0],
                         "from": key[1], "to": key[2], "via": "doc"}
    return {"schema_version": SCHEMA_VERSION, "id": "inferred", "name": "Inferred from graph",
            "version": 1, "dataset": "data/dishes.jsonl",
            "created_at": "2026-08-25T00:00:00Z", "updated_at": "2026-08-25T00:00:00Z",
            "entity_types": ents, "relation_types": list(rels.values()), "layout": {}}


# ---- the DSL -------------------------------------------------------------------------

# Precedence encoded by the alternation order: a ( … ) group beats a bare run so a value list
# never leaks its ')'; a quoted run beats an unquoted run so "Salads & Bowls" stays one value;
# `tag op value` beats `bare` so price_pp<=15 is never a word; the longest cmp_op wins.
_TOKEN_RE = re.compile(r'''
    \s*(?:
      (?P<neg>[-!])?
      (?:
         (?P<tag>[A-Za-z_][A-Za-z0-9_]*)\s*(?P<op><=|>=|<|>|=|:)\s*
         (?P<val>
              \( [^)]* \)
            | " (?:[^"\\]|\\.)* "
            | ' (?:[^'\\]|\\.)* '
            | [^\s"']+
         )
       | (?P<bare> " (?:[^"\\]|\\.)* " | ' (?:[^'\\]|\\.)* ' | [^\s"']+ )
      )
    )''', re.X)

_OP_MAP = {"<=": "lte", ">=": "gte", "<": "lt", ">": "gt", "=": "eq"}
_OP_TEXT = {"lte": "<=", "gte": ">=", "lt": "<", "gt": ">", "eq": "="}
_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return re.sub(r"\\(.)", r"\1", s[1:-1])
    return s


def _split_bar(s: str) -> list[str]:
    """Split on | OUTSIDE quotes, so ingredient:("pine nut"|peanut) works."""
    out, buf, q = [], [], ""
    for ch in s:
        if q:
            buf.append(ch)
            if ch == q:
                q = ""
        elif ch in "\"'":
            q = ch
            buf.append(ch)
        elif ch == "|":
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return [_unquote(x.strip()) for x in out]


def _quote_if_needed(v: str) -> str:
    s = str(v)
    return f'"{s}"' if re.search(r'[\s|)"\']', s) else s


def parse(src: str, index: KGIndex | None = None) -> dict:
    """Text -> the parsed query object the chips bind to. Typing and clicking are the same act,
    so this must round-trip losslessly with render()."""
    src = src or ""
    slots: list[dict] = []
    free_text: list[str] = []
    exclude_text: list[str] = []
    warnings: list[dict] = []
    suggested: list[dict] = []
    known_tags = set(index.slots) if index else set()

    pos, sid = 0, 0
    while pos < len(src):
        m = _TOKEN_RE.match(src, pos)
        if not m or m.end() == m.start():
            break                                   # zero-length match would spin forever
        pos = m.end()
        neg = bool(m.group("neg"))
        span = [m.start(), m.end()]
        bare = m.group("bare")

        if bare is not None:
            word = _unquote(bare)
            if not word:
                continue
            (exclude_text if neg else free_text).append(word)
            if index and not neg:
                hits = index.value_to_tags.get(_vkey(word), ())
                if hits:
                    # A bare word that IS a value somewhere becomes a SUGGESTION, never an
                    # auto-applied slot: `chicken` is both dish_type:poultry (20 rows) and
                    # ingredient:chicken (80 rows), and a buyer cares which.
                    suggested.append({"word": word, "tags": list(hits),
                                      "options": [{"tag": t, "value": index.values[t][_vkey(word)]["label"],
                                                   "count": len(index.postings[t][_vkey(word)])}
                                                  for t in hits]})
            continue

        raw_tag = (m.group("tag") or "").lower()
        tag = ALIASES.get(raw_tag, raw_tag)
        op_sym = m.group("op")
        raw = m.group("val") or ""

        if op_sym not in (":", "="):
            operand = _unquote(raw)
            if not _NUM_RE.match(operand):
                free_text.append(src[m.start():m.end()].strip())   # demote, don't reject
                continue
            slots.append({"id": f"s{sid}", "tag": tag, "kind": "measure",
                          "op": _OP_MAP[op_sym], "values": [{"value": float(operand)}],
                          "span": span})
            sid += 1
            continue

        if raw.startswith("("):
            vals = _split_bar(raw[1:-1])
        elif raw[:1] in "\"'":
            vals = [_unquote(raw)]                  # a quoted value is NEVER split on |
        else:
            vals = _split_bar(raw)
        if vals and vals[0][:1] in "-!" and not neg:
            neg = True
            vals[0] = vals[0][1:]
        vals = [_collapse_ws(v.strip()) for v in vals if v.strip()]
        if not vals:
            continue

        if op_sym == "=" and index and tag in index.measures:
            slots.append({"id": f"s{sid}", "tag": tag, "kind": "measure", "op": "eq",
                          "values": [{"value": float(vals[0])}] if _NUM_RE.match(vals[0]) else [],
                          "span": span})
            sid += 1
            continue

        if known_tags and tag not in known_tags:
            free_text.append(src[m.start():m.end()].strip())
            warnings.append({"code": "unknown_tag", "tag": raw_tag,
                             "did_you_mean": _did_you_mean(
                                 raw_tag, list(known_tags) + list(ALIASES))})
            continue

        entries = []
        for v in vals:
            key = _vkey(v)
            if index and tag in index.values and key in index.values[tag]:
                info = index.values[tag][key]
                entries.append({"value": info["label"], "node": info["node"],
                                "label": info["label"], "status": "resolved",
                                "count": len(index.postings[tag][key])})
            else:
                sugg = []
                if index and tag in index.values:
                    sugg = [index.values[tag][k]["label"] for k in index.values[tag]
                            if k.startswith(key) or key in k][:3]
                entries.append({"value": v, "node": None, "label": v,
                                "status": "unresolved", "count": 0, "suggestions": sugg})
        slots.append({"id": f"s{sid}", "tag": tag, "kind": "entity",
                      "op": "not" if neg else "is", "match": "any",
                      "safety_critical": tag in SAFETY_CRITICAL_TAGS,
                      "values": entries, "span": span})
        sid += 1

    # THE ONE PLACE THIS FEATURE REFUSES TO ANSWER. Returning results for -allergen:"pine nut"
    # when `pine nut` is not in the ontology would present dishes as pine-nut-free on no
    # evidence whatsoever. A wrong answer here is a hospital visit, so we return nothing.
    blocked = False
    for s in slots:
        if s.get("op") == "not" and s.get("tag") in SAFETY_CRITICAL_TAGS:
            if any(v.get("status") == "unresolved" for v in s["values"]):
                blocked = True
                warnings.append({
                    "code": "unresolved_safety_exclusion", "tag": s["tag"],
                    "values": [v["value"] for v in s["values"] if v.get("status") == "unresolved"],
                    "message": (f"{s['tag']} value(s) not in the ontology. Refusing to answer "
                                f"rather than imply a safety guarantee the graph cannot make.")})

    parsed = {"source": src, "normalized": "", "slots": slots, "free_text": free_text,
              "exclude_text": exclude_text, "suggested_slots": suggested,
              "warnings": warnings, "blocked": blocked}
    parsed["normalized"] = render(parsed)
    return parsed


def parse_query(src: str, index: KGIndex | None = None) -> dict:
    """Alias kept because `parse` is a very common name to shadow at a call site."""
    return parse(src, index)


def _slot_text(s: dict) -> str:
    tag = s["tag"]
    if s["kind"] == "measure":
        v = s["values"][0]["value"] if s["values"] else 0
        v = int(v) if float(v).is_integer() else v
        return f"{tag}{_OP_TEXT.get(s['op'], '>=')}{v}"
    vals = [_quote_if_needed(v["value"]) for v in s["values"]]
    body = vals[0] if len(vals) == 1 else "(" + "|".join(vals) + ")"
    return f"{'-' if s['op'] == 'not' else ''}{tag}:{body}"


def render(parsed: dict) -> str:
    """Canonical serialization: includes -> measures -> excludes -> free text. Stable ordering
    is what lets a saved query, a chip row and a URL all be the same string."""
    slots = parsed.get("slots") or []
    inc = [s for s in slots if s["kind"] == "entity" and s["op"] != "not"]
    mea = [s for s in slots if s["kind"] == "measure"]
    exc = [s for s in slots if s["kind"] == "entity" and s["op"] == "not"]
    key = lambda s: (s["tag"], (s["values"][0].get("value") if s["values"] else ""))  # noqa: E731
    parts = [_slot_text(s) for s in sorted(inc, key=key)]
    parts += [_slot_text(s) for s in sorted(mea, key=lambda s: (s["tag"], s["op"]))]
    parts += [_slot_text(s) for s in sorted(exc, key=key)]
    parts += [_quote_if_needed(t) for t in parsed.get("free_text") or []]
    parts += ["-" + _quote_if_needed(t) for t in parsed.get("exclude_text") or []]
    return " ".join(parts)


def slots_to_parsed(slots: list[dict], index: KGIndex | None = None) -> dict:
    """Wire FilledSlot[] -> the same parsed object the text path produces, by rendering the
    chips back to DSL and re-parsing. One code path, so the two entry points cannot diverge."""
    parts = []
    for s in slots or []:
        st = {"tag": ALIASES.get(str(s.get("tag", "")).lower(), str(s.get("tag", "")).lower()),
              "kind": s.get("kind") or "entity", "op": s.get("op") or "is",
              "values": [{"value": v.get("value") if isinstance(v, dict) else v}
                         for v in (s.get("values") or [])]}
        if st["values"]:
            parts.append(_slot_text(st))
    return parse(" ".join(parts), index)


def concepts_to_dsl(concepts: dict, index: KGIndex | None = None) -> str:
    """Bridge from the existing /api/understand. Its output becomes EDITABLE chips, which is
    the whole point — a query-understanding layer you can correct beats one you must accept."""
    parts: list[str] = []
    for key, (tag, op) in _CONCEPT_TO_SLOT.items():
        vals = concepts.get(key) or []
        if isinstance(vals, str):
            vals = [vals]
        if index and tag not in index.slots:
            continue                          # the model is the authority on what exists
        for v in vals:
            parts.append(f"{'-' if op == 'not' else ''}{tag}:{_quote_if_needed(v)}")
    for key, (tag, op) in _CONCEPT_TO_MEASURE.items():
        v = concepts.get(key)
        if v in (None, ""):
            continue
        if index and tag not in index.measures:
            continue
        parts.append(f"{tag}{_OP_TEXT[op]}{v}")
    tail = (concepts.get("free_text") or concepts.get("text") or "").strip()
    if tail:
        parts.append(tail)
    return " ".join(parts)


_CONCEPT_TO_SLOT = {
    "dietary":             ("diet", "is"),
    "exclude_allergens":   ("allergen", "not"),
    "exclude_ingredients": ("ingredient", "not"),
    "include":             ("ingredient", "is"),
    "cuisine":             ("cuisine", "is"),
    "occasion":            ("occasion", "is"),
}
_CONCEPT_TO_MEASURE = {
    "spice_min":    ("spice", "gte"),
    "max_price_pp": ("price_pp", "lte"),
    "headcount":    ("serves", "gte"),
}


# ---- execution -----------------------------------------------------------------------

def _chip_set(slot: dict, index: KGIndex) -> frozenset:
    """Union of a chip's values. OR lives INSIDE a chip; AND lives between chips — including
    between two chips carrying the same tag, which is the only way to express intra-tag AND."""
    tag = slot["tag"]
    posts = index.postings.get(tag, {})
    out: set = set()
    for v in slot["values"]:
        key = _vkey(v.get("value"))
        out |= posts.get(key, frozenset())
    return frozenset(out)


def _measure_ok(val: float, op: str, ref: float) -> bool:
    return {"lte": val <= ref, "gte": val >= ref, "lt": val < ref,
            "gt": val > ref, "eq": val == ref}.get(op, True)


def _apply(parsed: dict, index: KGIndex) -> dict:
    """Run the filter pipeline. The ORDER is load-bearing:

      universe -> positive entity (ascending set size) -> measures -> free text -> negatives

    Positives ascend by size so the intersection shrinks fastest; negatives run LAST so the
    step counter can report how many the exclusion removed *from the qualified set* rather
    than from the whole corpus.
    """
    slots = parsed.get("slots") or []
    R = set(index.universe)
    steps = [{"op": "universe", "in": len(R), "out": len(R), "removed": 0}]
    warnings: list[dict] = []

    inc = [s for s in slots if s["kind"] == "entity" and s["op"] != "not"]
    mea = [s for s in slots if s["kind"] == "measure"]
    exc = [s for s in slots if s["kind"] == "entity" and s["op"] == "not"]

    sized = []
    for s in inc:
        S = _chip_set(s, index)
        if not S and all(v.get("status") == "unresolved" for v in s["values"]):
            # Do NOT silently drop it. An ignored constraint is a wrong answer wearing a
            # confident face; an empty result plus a relaxation ladder is recoverable.
            warnings.append({"code": "unresolved_slot", "tag": s["tag"],
                             "values": [v["value"] for v in s["values"]]})
        sized.append((len(S), s, S))
    sized.sort(key=lambda x: (x[0], x[1]["tag"]))
    for _, s, S in sized:
        before = len(R)
        R &= S
        steps.append({"op": "include", "tag": s["tag"], "slot_id": s["id"],
                      "values": [v["value"] for v in s["values"]],
                      "in": before, "out": len(R), "removed": before - len(R)})
    after_include = len(R)

    for s in mea:
        tag, op = s["tag"], s["op"]
        ref = float(s["values"][0]["value"]) if s["values"] else 0.0
        series = index.measures.get(tag) or {}
        before = len(R)
        missing = {sid for sid in R if sid not in series}
        if missing:
            warnings.append({"code": "measure_missing", "tag": tag, "subjects": len(missing)})
        R = {sid for sid in R if sid in series and _measure_ok(series[sid], op, ref)}
        steps.append({"op": "measure", "tag": tag, "slot_id": s["id"],
                      "expr": f"{tag}{_OP_TEXT.get(op, '>=')}{ref:g}",
                      "in": before, "out": len(R), "removed": before - len(R)})
    after_measure = len(R)

    pos_tokens = [t for t in parsed.get("free_text") or []]
    if pos_tokens:
        before = len(R)
        for phrase in pos_tokens:
            words = _WORD_RE.findall(phrase.lower())
            live = [w for w in words if w in index.idf]
            if not live:
                # a token nobody has is a typo, not a filter — drop it here, keep it for scoring
                warnings.append({"code": "text_no_match", "term": phrase})
                continue
            R = {sid for sid in R if all(w in index.tokens[sid] for w in live)}
        steps.append({"op": "text", "terms": pos_tokens,
                      "in": before, "out": len(R), "removed": before - len(R)})
    for phrase in parsed.get("exclude_text") or []:
        before = len(R)
        p = phrase.lower()
        R = {sid for sid in R if p not in index.text[sid]}
        steps.append({"op": "exclude_text", "term": phrase,
                      "in": before, "out": len(R), "removed": before - len(R)})
    after_text = len(R)

    qualified = set(R)                       # everything that passed before the exclusions run
    blocked_by: dict[str, list[str]] = {}
    for s in exc:
        S = _chip_set(s, index)
        before = len(R)
        removed = R & S
        for sid in removed:
            blocked_by.setdefault(sid, []).extend(
                v.get("node") or f"{s['tag']}:{_vkey(v.get('value'))}" for v in s["values"])
        R -= S
        steps.append({"op": "exclude", "tag": s["tag"], "slot_id": s["id"],
                      "values": [v["value"] for v in s["values"]],
                      "in": before, "out": len(R), "removed": before - len(R)})

    return {"R": R, "steps": steps, "warnings": warnings, "qualified": qualified,
            "blocked_by": blocked_by,
            "counts": {"universe": len(index.universe), "after_include": after_include,
                       "after_measure": after_measure, "after_text": after_text,
                       "after_exclude": len(R)}}


def score(sid: str, parsed: dict, index: KGIndex, ctx: Any = None) -> tuple[float, dict]:
    """No relevance model exists for this corpus, so the arithmetic is small, explainable and
    shown to the user. Weights renormalize over the ACTIVE parts only — a query with no free
    text does not quietly lose 20% of its score ceiling."""
    ctx = ctx or {}
    slots = parsed.get("slots") or []
    parts: dict[str, float] = {}
    weights: dict[str, float] = {}

    positives = [s for s in slots if s["op"] not in ("not",)]
    if positives:
        hit = 0
        for s in positives:
            if s["kind"] == "measure":
                hit += 1                      # a measure that survived the filter is satisfied
            elif _chip_set(s, index) & {sid}:
                hit += 1
        parts["slot_coverage"] = hit / len(positives)
    else:
        parts["slot_coverage"] = 1.0
    weights["slot_coverage"] = 0.40

    pop = float((index.attrs.get(sid) or {}).get("popularity") or 0)
    parts["popularity"] = max(0.0, min(pop / 100.0, 1.0))
    weights["popularity"] = 0.25

    if ctx.get("text_scores") is not None:
        parts["text"] = ctx["text_scores"].get(sid, 0.0)
        weights["text"] = 0.20

    if ctx.get("safety_exclusions"):
        # Fewer allergens overall = more headroom for the guests nobody warned you about.
        n_alg = len(index.subject_values.get(sid, {}).get("allergen", ()))
        parts["allergen_headroom"] = max(0.0, 1.0 - n_alg / max(ctx.get("max_allergens", 1), 1))
        weights["allergen_headroom"] = 0.15

    need = ctx.get("serves_floor")
    if need:
        serves = float((index.measures.get("serves") or {}).get(sid) or 0)
        # prefer the SMALLEST sufficient tray — a 50-serve order for 25 guests wastes half of it
        parts["serves_fit"] = min(need / serves, 1.0) if serves > 0 else 0.0
        weights["serves_fit"] = 0.15

    total_w = sum(weights.values()) or 1.0
    total = sum(parts[k] * weights[k] for k in parts) / total_w
    return round(total, 4), {k: round(v, 4) for k, v in parts.items()}


def _bm25(parsed: dict, R: set, index: KGIndex) -> dict[str, float] | None:
    terms = [w for phrase in (parsed.get("free_text") or [])
             for w in _WORD_RE.findall(phrase.lower())]
    if not terms:
        return None
    raw: dict[str, float] = {}
    k1, b = 1.2, 0.75
    for sid in R:
        blob = index.text[sid]
        dl = index.doc_len[sid]
        s = 0.0
        for w in terms:
            tf = blob.count(w)
            if not tf:
                continue
            s += index.idf.get(w, 0.0) * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / index.avg_len))
        raw[sid] = s
    if not raw:
        return {}
    lo, hi = min(raw.values()), max(raw.values())
    span = (hi - lo) or 1.0
    return {k: (v - lo) / span for k, v in raw.items()}


def _row(sid: str, parsed: dict, index: KGIndex, sc: float, parts: dict) -> dict:
    a = index.attrs.get(sid) or {}
    sv = index.subject_values.get(sid, {})
    slots_out = {t: sorted(index.values[t][v]["label"] for v in vs)
                 for t, vs in sv.items() if vs}
    matched, cleared = [], []
    for s in parsed.get("slots") or []:
        if s["kind"] != "entity":
            continue
        for v in s["values"]:
            key = _vkey(v.get("value"))
            node = v.get("node") or f"{s['tag']}:{key}"
            if s["op"] == "not":
                # what the row was CHECKED AGAINST and survived — this is what lets a card say
                # "nut-free ✓ (checked against allergen:nuts)" instead of merely omitting nuts
                cleared.append({"tag": s["tag"], "value": v.get("value"), "node": node})
            elif key in sv.get(s["tag"], ()):
                rel = index.slots.get(s["tag"]).relation if s["tag"] in index.slots else None
                matched.append({"tag": s["tag"], "value": v.get("value"), "node": node,
                                "rel": (rel or {}).get("name"),
                                "direction": (rel or {}).get("direction", "out")})
    return {
        "dish_id": a.get("id") or sid.split(":", 1)[-1], "node": sid,
        "name": index.g.nodes[sid].get("label") if index.g is not None else a.get("name"),
        "base_name": index.base.get(sid, ""),
        "description": a.get("desc", ""), "caterer": a.get("caterer", ""),
        "caterer_count": 1, "variants": [a.get("id") or sid.split(":", 1)[-1]],
        "price_pp": a.get("price_pp"), "price_pp_range": None,
        "serves": a.get("serves"), "serves_max": a.get("serves"),
        "popularity": a.get("popularity"), "spice_level": a.get("spice_level"),
        "slots": slots_out, "score": sc, "score_parts": parts,
        "matched": matched, "cleared": cleared, "img": a.get("img"),
    }


def _rollup(rows: list[dict], index: KGIndex) -> list[dict]:
    """600 rows are 60 base dishes x 9-10 caterers. Twelve cards of Vegetable Tempura is a bad
    answer, so the default is to collapse to the base dish and carry the spread along."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["base_name"] or r["name"], []).append(r)
    out = []
    for _, members in groups.items():
        rep = dict(members[0])
        prices = [m["price_pp"] for m in members if isinstance(m["price_pp"], (int, float))]
        serves = [m["serves"] for m in members if isinstance(m["serves"], (int, float))]
        rep["caterer_count"] = len({m["caterer"] for m in members})
        rep["variants"] = [m["dish_id"] for m in members][:12]
        rep["price_pp_range"] = [round(min(prices), 2), round(max(prices), 2)] if prices else None
        rep["serves_max"] = max(serves) if serves else rep.get("serves")
        out.append(rep)
    return out


def _entropy_ratio(counts: list[int]) -> float:
    tot = sum(counts)
    if tot <= 0 or len(counts) < 2:
        return 0.0
    h = -sum((c / tot) * math.log(c / tot) for c in counts if c)
    return h / math.log(len(counts))


def _missing_slots(parsed: dict, R: set, index: KGIndex, limit: int = 8) -> list[dict]:
    """The most USEFUL next question, not merely the biggest facet — ranked by normalized
    Shannon entropy, so a facet that actually splits the result set floats to the top."""
    used = {s["tag"] for s in parsed.get("slots") or []}
    out: list[dict] = []
    for tag, slot in index.slots.items():
        if tag in used or slot.kind == "text":
            continue
        if slot.kind == "measure":
            series = index.measures.get(tag) or {}
            vals = sorted(series[s] for s in R if s in series)
            if len(vals) < 2:
                continue
            lo, hi = vals[0], vals[-1]
            step = (hi - lo) / 5 or 1.0
            hist = []
            for i in range(5):
                a, b_ = lo + i * step, lo + (i + 1) * step
                c = sum(1 for v in vals if (a <= v < b_) or (i == 4 and v == hi))
                hist.append({"lo": round(a, 2), "hi": round(b_, 2), "count": c})
            out.append({"tag": tag, "label": slot.label, "kind": "measure",
                        "min": round(lo, 2), "max": round(hi, 2),
                        "median": round(vals[len(vals) // 2], 2),
                        "histogram": hist, "discriminative": 0.5})
            continue
        posts = index.postings.get(tag, {})
        counts = []
        for v, ids in posts.items():
            n = len(ids & R)
            if n:
                counts.append((n, v))
        if not counts:
            continue
        counts.sort(key=lambda x: (-x[0], x[1]))
        covered = len({s for v, ids in posts.items() for s in (ids & R)})
        out.append({
            "tag": tag, "label": slot.label, "kind": "entity",
            "coverage_in_result": round(covered / max(len(R), 1), 4),
            "discriminative": round(_entropy_ratio([c for c, _ in counts]), 4),
            "values": [{"value": index.values[tag][v]["label"], "node": index.values[tag][v]["node"],
                        "label": index.values[tag][v]["label"], "count": c}
                       for c, v in counts[:limit]],
        })
    out.sort(key=lambda x: -x.get("discriminative", 0))
    return out


def relaxation_ladder(parsed: dict, index: KGIndex, max_level: int = 2) -> list[dict]:
    """Leave-one-out then leave-two-out over the relaxable constraints. <=36 re-executions at
    ~1 ms each. Safety-critical exclusions are computed for transparency but LOCKED: the count
    is shown, the button is never offered."""
    slots = parsed.get("slots") or []
    if not slots and not parsed.get("free_text"):
        return []
    items = []
    for s in slots:
        locked = s["kind"] == "entity" and s["op"] == "not" and s["tag"] in SAFETY_CRITICAL_TAGS
        items.append({"slot": s, "label": _slot_text(s), "locked": locked})
    out: list[dict] = []

    def run(drop_ids: set) -> int:
        sub = {**parsed, "slots": [s for s in slots if s["id"] not in drop_ids]}
        return len(_apply(sub, index)["R"])

    for it in items:
        out.append({"level": 1, "drop": [it["label"]], "label": it["label"],
                    "count": run({it["slot"]["id"]}), "locked": it["locked"],
                    **({"reason": "safety_critical"} if it["locked"] else {})})
    if max_level >= 2 and len(items) >= 2:
        pairs = 0
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                if pairs >= 24:
                    break
                a, b_ = items[i], items[j]
                pairs += 1
                lock = a["locked"] or b_["locked"]
                out.append({"level": 2, "drop": [a["label"], b_["label"]],
                            "label": f"{a['label']} + {b_['label']}",
                            "count": run({a["slot"]["id"], b_["slot"]["id"]}),
                            "locked": lock,
                            **({"reason": "safety_critical"} if lock else {})})
    out.sort(key=lambda e: (e["level"], -e["count"]))
    out = [e for e in out if e["count"] > 0 or e["level"] == 1]
    out.append({"exhausted": all(e.get("count", 0) == 0 for e in out)})
    return out


SUBGRAPH_MAX_NODES, SUBGRAPH_MAX_EDGES = 120, 240
_LEGEND = {
    "include_hit": {"label": "asked for — matched", "color": "#2e7d32", "shape": "box"},
    "include_miss": {"label": "asked for — nothing matched", "color": "#b0854a", "dashes": True},
    "exclude": {"label": "excluded", "color": "#c62828", "dashes": True},
    "result": {"label": "result", "color": "#e35205"},
    "blocked": {"label": "removed by an exclusion", "color": "#9aa0a6", "opacity": 0.35},
    "context": {"label": "nearby — click to add a slot", "color": "#7c8698"},
}


def build_subgraph(parsed: dict, R: set, ranked: list[tuple[str, float, dict]], index: KGIndex,
                   g: nx.DiGraph, *, top: int = 12, blocked: int = 8, context: int = 12,
                   qualified: set | None = None, blocked_by: dict | None = None) -> dict:
    """Results plus the SHAPE of the constraint. The `blocked` nodes — subjects that satisfied
    every positive constraint and were then removed by a negative one, drawn faded next to the
    exclusion node — are the entire difference between a filter and a graph."""
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    focus: list[str] = []
    trunc = {"results": 0, "blocked": 0, "context": 0}

    for s in parsed.get("slots") or []:
        if s["kind"] != "entity":
            continue
        for v in s["values"]:
            node = v.get("node")
            if not node or node not in g:
                continue
            focus.append(node)
            if s["op"] == "not":
                role, weight = "exclude", len((_chip_set(s, index) & (qualified or set())))
            else:
                hits = len(_chip_set(s, index) & R)
                role, weight = ("include_hit" if hits else "include_miss"), hits
            nodes[node] = {"id": node, "kind": g.nodes[node].get("kind"),
                           "label": g.nodes[node].get("label"), "role": role,
                           "tag": s["tag"], "weight": weight, "slot_id": s["id"]}

    top_ids = [sid for sid, _, _ in ranked[:top]]
    trunc["results"] = len(top_ids)
    for rank, sid in enumerate(top_ids, 1):
        nodes[sid] = {"id": sid, "kind": g.nodes[sid].get("kind"),
                      "label": g.nodes[sid].get("label"), "role": "result",
                      "rank": rank, "score": ranked[rank - 1][1]}
        for s in parsed.get("slots") or []:
            if s["kind"] != "entity" or s["op"] == "not":
                continue
            for v in s["values"]:
                node = v.get("node")
                if node and g.has_edge(sid, node):
                    edges.append({"from": sid, "to": node, "rel": g.edges[sid, node].get("rel"),
                                  "role": "matched", "slot_id": s["id"]})
                elif node and g.has_edge(node, sid):
                    edges.append({"from": node, "to": sid, "rel": g.edges[node, sid].get("rel"),
                                  "role": "matched", "slot_id": s["id"]})

    bb = blocked_by or {}
    sample = sorted(bb, key=lambda s: -float((index.attrs.get(s) or {}).get("popularity") or 0))[:blocked]
    trunc["blocked"] = len(sample)
    for sid in sample:
        nodes[sid] = {"id": sid, "kind": g.nodes[sid].get("kind"),
                      "label": g.nodes[sid].get("label"), "role": "blocked",
                      "blocked_by": sorted(set(bb[sid]))}
        for node in set(bb[sid]):
            if g.has_edge(sid, node):
                edges.append({"from": sid, "to": node, "rel": g.edges[sid, node].get("rel"),
                              "role": "blocked"})

    used_tags = {s["tag"] for s in parsed.get("slots") or []}
    ctx_added = 0
    for sid in top_ids:
        for _, t in g.out_edges(sid):
            k = g.nodes[t].get("kind")
            if t in nodes or k in used_tags or k == index.subject:
                continue
            if ctx_added >= context:
                break
            nodes[t] = {"id": t, "kind": k, "label": g.nodes[t].get("label"), "role": "context",
                        "tag": k, "weight": g.nodes[t].get("n", 0), "suggest": True}
            ctx_added += 1
        for _, t in g.out_edges(sid):
            if t in nodes and nodes[t]["role"] == "context":
                edges.append({"from": sid, "to": t, "rel": g.edges[sid, t].get("rel"),
                              "role": "context"})
    trunc["context"] = ctx_added

    # vis-network's barnesHut degrades badly past ~150 nodes, so drop in a defined order:
    # context first (decorative), then blocked, then the tail of the results.
    if len(nodes) > SUBGRAPH_MAX_NODES:
        for role in ("context", "blocked", "result"):
            for nid in [k for k, v in nodes.items() if v["role"] == role][::-1]:
                if len(nodes) <= SUBGRAPH_MAX_NODES:
                    break
                nodes.pop(nid, None)
    keep = set(nodes)
    edges = [e for e in edges if e["from"] in keep and e["to"] in keep][:SUBGRAPH_MAX_EDGES]
    return {"nodes": list(nodes.values()), "edges": edges, "focus": focus, "legend": _LEGEND,
            "truncated": trunc, "stats": {"nodes": len(nodes), "edges": len(edges)}}


def execute(parsed: dict, index: KGIndex, *, limit: int = 24, offset: int = 0,
            rollup: bool = True, facets: bool = True, subgraph: bool = True) -> dict:
    t0 = time.perf_counter()
    base = {"ok": True, "blocked": False, "model_id": index.model_id,
            "index_version": index.version, "dsl": parsed.get("source", ""),
            "normalized_dsl": parsed.get("normalized", ""), "parse": parsed}

    if parsed.get("blocked"):
        # HTTP 200, empty rows. A blocked query is an answer ("I will not assert that"), not
        # a client error.
        return {**base, "blocked": True, "rows": [],
                "counts": {"universe": len(index.universe), "after_include": 0,
                           "after_measure": 0, "after_text": 0, "after_exclude": 0,
                           "returned": 0, "distinct": 0, "rolled_up_from": 0},
                "steps": [], "missing_slots": [], "relaxation": [],
                "subgraph": {"nodes": [], "edges": [], "focus": [], "legend": _LEGEND,
                             "truncated": {"results": 0, "blocked": 0, "context": 0},
                             "stats": {"nodes": 0, "edges": 0}},
                "warnings": parsed.get("warnings", []),
                "timing_ms": {"parse": 0.0, "execute": 0.0, "rank": 0.0, "facets": 0.0,
                              "subgraph": 0.0,
                              "total": round((time.perf_counter() - t0) * 1000, 2)}}

    t_ex = time.perf_counter()
    res = _apply(parsed, index)
    R = res["R"]
    ms_exec = (time.perf_counter() - t_ex) * 1000

    t_rank = time.perf_counter()
    ctx: dict = {"text_scores": _bm25(parsed, R, index)}
    safety = [s for s in parsed.get("slots") or []
              if s["op"] == "not" and s["tag"] in SAFETY_CRITICAL_TAGS]
    if safety:
        ctx["safety_exclusions"] = True
        ctx["max_allergens"] = max((len(v.get("allergen", ())) for v in
                                    index.subject_values.values()), default=1) or 1
    floor = next((float(s["values"][0]["value"]) for s in parsed.get("slots") or []
                  if s["kind"] == "measure" and s["tag"] == "serves" and s["op"] in ("gte", "gt")
                  and s["values"]), None)
    if floor:
        ctx["serves_floor"] = floor

    scored = [(sid, *score(sid, parsed, index, ctx)) for sid in R]
    # DETERMINISTIC tiebreak. frozenset iteration order is hash-dependent; without this the
    # same query would reorder between runs and the demo would visibly flicker.
    scored.sort(key=lambda x: (-x[1], -float((index.attrs.get(x[0]) or {}).get("popularity") or 0),
                               index.base.get(x[0], ""), x[0]))
    rows = [_row(sid, parsed, index, sc, parts) for sid, sc, parts in scored]
    pre_rollup = len(rows)
    if rollup:
        rows = _rollup(rows, index)
    distinct = len(rows)
    page = rows[offset:offset + limit]
    ms_rank = (time.perf_counter() - t_rank) * 1000

    t_f = time.perf_counter()
    missing = _missing_slots(parsed, R, index) if facets else []
    relax = relaxation_ladder(parsed, index) if (not R and (parsed.get("slots")
                                                           or parsed.get("free_text"))) else []
    ms_f = (time.perf_counter() - t_f) * 1000

    t_s = time.perf_counter()
    sg = (build_subgraph(parsed, R, scored, index, index.g,
                         qualified=res["qualified"], blocked_by=res["blocked_by"])
          if subgraph and index.g is not None else
          {"nodes": [], "edges": [], "focus": [], "legend": _LEGEND,
           "truncated": {"results": 0, "blocked": 0, "context": 0},
           "stats": {"nodes": 0, "edges": 0}})
    ms_s = (time.perf_counter() - t_s) * 1000

    counts = {**res["counts"], "returned": len(page), "distinct": distinct,
              "rolled_up_from": pre_rollup}
    return {**base, "counts": counts, "steps": res["steps"], "rows": page,
            "missing_slots": missing, "relaxation": relax, "subgraph": sg,
            "warnings": parsed.get("warnings", []) + res["warnings"],
            "timing_ms": {"parse": 0.0, "execute": round(ms_exec, 2), "rank": round(ms_rank, 2),
                          "facets": round(ms_f, 2), "subgraph": round(ms_s, 2),
                          "total": round((time.perf_counter() - t0) * 1000, 2)}}


def run_query(g: nx.DiGraph, model: dict, q: Any, **kwargs) -> dict:
    """The one-call entry point: graph + model + (DSL string | parsed object | FilledSlot[]).

    Accepts all three shapes because the bar, the chips and a saved query all arrive here.
    """
    key = model.get("id") or "adhoc"
    idx = _INDEX_CACHE.get(key)
    want = index_version(model)
    if idx is None or idx.version != want or idx.g is not g:
        idx = build_index(g, model)
        _INDEX_CACHE[key] = idx

    if isinstance(q, str):
        parsed = parse(q, idx)
    elif isinstance(q, list):
        parsed = slots_to_parsed(q, idx)
    elif isinstance(q, dict) and "slots" in q:
        # re-parse from source so a query parsed WITHOUT an index gets its values resolved
        parsed = parse(q.get("source") or render(q), idx)
    else:
        parsed = parse(str(q or ""), idx)
    return execute(parsed, idx, **kwargs)


# ---- typeahead -----------------------------------------------------------------------

def caret_context(src: str, caret: int) -> dict:
    """What is the caret sitting inside? Everything the typeahead does keys off this."""
    caret = max(0, min(caret if caret is not None else len(src), len(src)))
    left = src[:caret]
    start = max(left.rfind(" ") + 1, 0)
    token = src[start:caret]
    neg = token[:1] in "-!"
    body = token[1:] if neg else token
    if ":" in body:
        tag, _, val = body.partition(":")
        raw_tag = tag.lower()
        return {"mode": "value", "tag": ALIASES.get(raw_tag, raw_tag), "raw_tag": raw_tag,
                "prefix": val, "neg": neg,
                "replace": [caret - len(val), caret], "token_start": start}
    return {"mode": "bare", "tag": "", "raw_tag": "", "prefix": body, "neg": neg,
            "replace": [start, caret], "token_start": start}


def typeahead(src: str, caret: int, index: KGIndex, *, tag: str = "",
              mode: str = "auto", limit: int = 8) -> dict:
    t0 = time.perf_counter()
    ctx = caret_context(src or "", caret if caret is not None else len(src or ""))
    if mode == "auto":
        mode = ctx["mode"] if not tag else "value"
    active_tag = tag or ctx["tag"]
    prefix = _vkey(ctx["prefix"])
    used = {s["tag"] for s in parse(src or "", index).get("slots") or []}
    sugg: list[dict] = []

    def value_rows(t: str, cap: int) -> list[tuple]:
        rows = []
        slot = index.slots.get(t)
        color = slot.color if slot else "#9aa0a6"
        for v, info in (index.values.get(t) or {}).items():
            n = len(index.postings[t][v])
            if not prefix:
                tier = 1
            elif v == prefix:
                tier = 0
            elif v.startswith(prefix):
                tier = 1
            elif any(w.startswith(prefix) for w in v.split()):
                tier = 2
            elif prefix in v:
                tier = 3
            else:
                continue
            rows.append((tier, -n, len(info["label"]), info["label"],
                         {"kind": "value", "tag": t, "value": info["label"],
                          "label": info["label"], "node": info["node"], "count": n,
                          "insert": _quote_if_needed(info["label"]),
                          "match": ("exact", "prefix", "word", "substring")[tier],
                          "color": color}))
        rows.sort(key=lambda r: r[:4])
        return rows[:cap]

    if mode == "value" and active_tag in index.values:
        sugg = [r[4] for r in value_rows(active_tag, limit)]
    elif mode == "tag" or (mode == "bare" and not prefix):
        for t, slot in sorted(index.slots.items(), key=lambda kv: kv[1].order):
            if slot.kind == "text" or (prefix and not t.startswith(prefix)
                                       and not slot.label.lower().startswith(prefix)):
                continue
            sugg.append({"kind": "tag", "tag": t, "label": slot.label,
                         "insert": f"{t}:", "count": slot.value_count,
                         "used": t in used, "color": slot.color,
                         "match": "prefix"})
        sugg = sugg[:limit]
    else:
        # Bare mode: cap 3 per tag BEFORE merging. Without the cap `caterer` (96 values sharing
        # tokens like "Thai" and "Kitchen") floods every prefix and buries cuisine:Thai.
        merged: list[tuple] = []
        for t in index.values:
            merged.extend(value_rows(t, 3))
        merged.sort(key=lambda r: r[:4])
        sugg = [r[4] for r in merged[:limit]]
        for t, slot in index.slots.items():
            if slot.kind == "entity" and t.startswith(prefix) and len(sugg) < limit:
                sugg.append({"kind": "tag", "tag": t, "label": slot.label, "insert": f"{t}:",
                             "count": slot.value_count, "color": slot.color, "match": "tag"})
        if prefix:
            sugg.append({"kind": "free", "tag": "text", "value": ctx["prefix"],
                         "label": f'search text "{ctx["prefix"]}"', "insert": ctx["prefix"],
                         "match": "free"})

    return {"ok": True, "mode": mode, "tag": active_tag, "prefix": ctx["prefix"],
            "replace": ctx["replace"], "suggestions": sugg[:limit],
            "took_ms": round((time.perf_counter() - t0) * 1000, 2)}


def values_for_tag(index: KGIndex, tag: str, q: str = "", limit: int = 10) -> list[dict]:
    """Backing for GET /api/kg/values. `insert` is pre-quoted HERE, on the server, so the
    client can concatenate blindly and can never emit an unparseable string."""
    tag = ALIASES.get((tag or "").lower(), (tag or "").lower())
    prefix = _vkey(q)
    rows = []
    for v, info in (index.values.get(tag) or {}).items():
        if prefix and not (v.startswith(prefix) or prefix in v):
            continue
        rows.append({"value": info["label"], "label": info["label"], "node": info["node"],
                     "count": len(index.postings[tag][v]),
                     "match": "prefix" if v.startswith(prefix) else "substring",
                     "insert": _quote_if_needed(info["label"])})
    rows.sort(key=lambda r: (0 if r["match"] == "prefix" else 1, -r["count"], r["label"]))
    return rows[:limit]


# ═══════════════════════════════════════════════════════════════════════════════════════
# ANALYTICS — the six questions a flat filter cannot answer
#
# All of these are O(N·k) set arithmetic or a single networkx pass. None calls Vespa, none
# calls an LLM, none takes longer than a couple of milliseconds.
# ═══════════════════════════════════════════════════════════════════════════════════════

def _base_set(index: KGIndex, base: str) -> set:
    if not (base or "").strip():
        return set(index.universe)
    return _apply(parse(base, index), index)["R"]


def analytics_coverage(index: KGIndex, rows: str, cols: str, base: str = "",
                       min_count: int = 1) -> dict:
    """A cross-tab straight out of the postings. Turns "let me check 96 caterers" into
    "skip these five cuisines"."""
    rows = ALIASES.get(rows, rows)
    cols = ALIASES.get(cols, cols)
    B = _base_set(index, base)
    rvals = sorted(index.postings.get(rows, {}))
    cvals = sorted(index.postings.get(cols, {}),
                   key=lambda v: -len(index.postings[cols][v]))
    matrix, gaps, thin = [], [], []
    for r in rvals:
        Rr = index.postings[rows][r] & B
        line = []
        for c in cvals:
            n = len(Rr & index.postings[cols][c])
            line.append(n)
            if n == 0:
                gaps.append({"row": r, "col": c, "kind": "menu"})
            elif n < min_count:
                thin.append({"row": r, "col": c, "count": n})
        matrix.append(line)
    return {"ok": True, "rows": rows, "cols": cols, "base": base, "base_count": len(B),
            "row_values": rvals, "col_values": cvals, "matrix": matrix,
            "gaps": gaps, "thin": thin}


def analytics_blockers(index: KGIndex, base: str = "", tags: list[str] | None = None,
                       limit: int = 10) -> dict:
    """Which single exclusion costs the most. `unique_loss` counts subjects that have no
    replacement within their own dish_type — the losses you cannot substitute your way out of."""
    B = _base_set(index, base)
    tags = [ALIASES.get(t, t) for t in (tags or ["allergen", "ingredient"])]
    rollup_tag = "dish_type" if "dish_type" in index.postings else None
    out = []
    for t in tags:
        for v, ids in (index.postings.get(t) or {}).items():
            hit = ids & B
            if not hit:
                continue
            unique_loss = 0
            if rollup_tag:
                for sid in hit:
                    fam = index.subject_values.get(sid, {}).get(rollup_tag, ())
                    survivors: set = set()
                    for fv in fam:
                        survivors |= (index.postings[rollup_tag][fv] & B) - ids
                    if not survivors:
                        unique_loss += 1
            out.append({"tag": t, "value": index.values[t][v]["label"],
                        "node": index.values[t][v]["node"], "blast": len(hit),
                        "pct": round(len(hit) / max(len(B), 1), 4),
                        "unique_loss": unique_loss})
    out.sort(key=lambda r: (-r["blast"], r["tag"], r["value"]))
    return {"ok": True, "base": base, "base_count": len(B), "blockers": out[:limit]}


def _find_subject(index: KGIndex, needle: str) -> str | None:
    n = _vkey(needle)
    for sid in index.universe:
        if (index.attrs.get(sid) or {}).get("id") == needle or sid == needle:
            return sid
    hits = [sid for sid in index.universe if index.base.get(sid) == n]
    if not hits:
        hits = [sid for sid in index.universe
                if n in (index.g.nodes[sid].get("label") or "").lower()]
    if not hits:
        return None
    return sorted(hits, key=lambda s: (-float((index.attrs.get(s) or {}).get("popularity") or 0), s))[0]


def analytics_substitute(index: KGIndex, dish: str, keep: list[str] | None = None,
                         require: str = "", limit: int = 8) -> dict:
    """A restriction lands after the menu is set. The buyer does not want a new search — they
    want the nearest thing to what they already chose that clears the new constraint."""
    keep = [ALIASES.get(k, k) for k in (keep or ["dish_type", "cuisine"])]
    target = _find_subject(index, dish)
    if not target:
        raise OntologyError("NOT_FOUND", f"No dish matching {dish!r}.", detail={"dish": dish})
    req = _base_set(index, require)
    tv = index.subject_values.get(target, {})
    tset = {(t, v) for t, vs in tv.items() for v in vs}
    tbase = index.base.get(target)
    peers = {s for s in index.universe if index.base.get(s) == tbase}

    def members(tags: list[str]) -> set:
        out = set(index.universe)
        for t in tags:
            vs = tv.get(t) or ()
            got: set = set()
            for v in vs:
                got |= index.postings[t][v]
            out &= got
        return (out & req) - peers

    course_tags = [t for t in ("cuisine", "course") if t in index.postings]
    levels = [
        (1, f"same {keep[0]} + same {keep[-1]}", members(keep)),
        (2, f"same {keep[0]}, any {keep[-1]}", members(keep[:1])),
        (3, "same cuisine + same course", members(course_tags)),
    ]
    out_levels = []
    seen: set = set()
    for lvl, rule, ids in levels:
        rows = []
        for sid in ids - seen:
            sv = {(t, v) for t, vs in index.subject_values.get(sid, {}).items() for v in vs}
            jac = len(tset & sv) / max(len(tset | sv), 1)
            rows.append({"dish_id": (index.attrs.get(sid) or {}).get("id"), "node": sid,
                         "name": index.g.nodes[sid].get("label"),
                         "base_name": index.base.get(sid),
                         "cuisine": (index.attrs.get(sid) or {}).get("cuisine"),
                         "jaccard": round(jac, 3),
                         "hops": [f"{keep[0]}:{v}" for v in (tv.get(keep[0]) or ())]})
        # dedupe to base names: ten caterers selling the same substitute is one suggestion
        best: dict[str, dict] = {}
        for r in sorted(rows, key=lambda r: (-r["jaccard"], r["base_name"] or "")):
            best.setdefault(r["base_name"], r)
        out_levels.append({"level": lvl, "rule": rule, "count": len(rows),
                           "rows": list(best.values())[:limit]})
        seen |= ids
    hops = [f"{index.subject}:{tbase}"]
    if tv.get(keep[0]):
        hops.append(f"{keep[0]}:{sorted(tv[keep[0]])[0]}")
    top = next((l["rows"][0] for l in out_levels if l["rows"]), None)
    if top:
        hops.append(f"{index.subject}:{top['base_name']}")
    return {"ok": True,
            "target": {"dish_id": (index.attrs.get(target) or {}).get("id"), "node": target,
                       "name": index.g.nodes[target].get("label"), "base_name": tbase,
                       "slots": {t: sorted(index.values[t][v]["label"] for v in vs)
                                 for t, vs in tv.items() if t in keep}},
            "require": require, "levels": out_levels, "hops": hops}


def analytics_bridge(index: KGIndex, g: nx.DiGraph, a: str, b: str,
                     via: list[str] | None = None, k: int = 6) -> dict:
    """What two concepts share and where they diverge — the shape of a substitution pitch."""
    via = [ALIASES.get(v, v) for v in (via or ["ingredient", "dish_type", "occasion", "diet"])]

    def resolve(spec: str) -> tuple[str, set]:
        tag, _, val = (spec or "").partition(":")
        tag = ALIASES.get(tag.lower(), tag.lower())
        key = _vkey(val)
        ids = (index.postings.get(tag) or {}).get(key, frozenset())
        return f"{tag}:{key}", set(ids)

    an, A = resolve(a)
    bn, B = resolve(b)
    out = []
    for t in via:
        va = {v for v, ids in (index.postings.get(t) or {}).items() if ids & A}
        vb = {v for v, ids in (index.postings.get(t) or {}).items() if ids & B}
        out.append({"tag": t, "shared": sorted(va & vb)[:k * 4],
                    "only_a": sorted(va - vb), "only_b": sorted(vb - va),
                    "jaccard": round(len(va & vb) / max(len(va | vb), 1), 3)})
    # The drawable path. A raw shortest_path picks whichever 4-hop route networkx happens to
    # find first — usually through a 540-member tag like diet:soy-free, which is true and
    # useless. Route through the MOST SPECIFIC shared concept instead: the rarer the connector,
    # the more it actually says about why these two cuisines are related.
    paths, path_labels = [], []
    best = None
    for entry in out:
        t = entry["tag"]
        for v in entry["shared"]:
            n = len(index.postings[t][v])
            if best is None or n < best[0]:
                best = (n, t, v)
    if best:
        _, t, v = best
        mid = f"{t}:{v}"
        pick = lambda side: sorted(  # noqa: E731
            index.postings[t][v] & side,
            key=lambda s: (-float((index.attrs.get(s) or {}).get("popularity") or 0), s))
        left, right = pick(A), pick(B)
        if left and right:
            paths.append([an, left[0], mid, right[0], bn])
            path_labels.append([an, g.nodes[left[0]].get("label"), mid,
                                g.nodes[right[0]].get("label"), bn])
    if not paths and an in g and bn in g:
        try:
            paths.append(nx.shortest_path(g.to_undirected(as_view=True), an, bn))
            path_labels.append([g.nodes[n].get("label") or n for n in paths[0]])
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            pass
    return {"ok": True, "a": an, "b": bn, "via": out, "paths": paths,
            "path_labels": path_labels}


def analytics_one_stop(index: KGIndex, require: list[str], group: str = "caterer",
                       limit: int = 12) -> dict:
    """Purchasing hates split orders. "Who covers ALL my constraints in one order" is a
    set-cover question, not a filter."""
    group = ALIASES.get(group, group)
    sets = [_base_set(index, r) for r in require]
    posts = index.postings.get(group) or {}
    rows = []
    for v, ids in posts.items():
        covers = [i for i, S in enumerate(sets) if ids & S]
        if not covers:
            continue
        members = ids & set().union(*sets) if sets else ids
        pops = [float((index.attrs.get(s) or {}).get("popularity") or 0) for s in members]
        rows.append({"value": index.values[group][v]["label"],
                     "node": index.values[group][v]["node"], "covers": covers,
                     "coverage": round(len(covers) / max(len(sets), 1), 4),
                     "dish_count": len(members),
                     "mean_popularity": round(sum(pops) / len(pops), 1) if pops else 0.0})
    rows.sort(key=lambda r: (-r["coverage"], -r["dish_count"], -r["mean_popularity"], r["value"]))
    full = [r for r in rows if r["coverage"] >= 1.0]
    min_cover = None
    if not full and rows:
        # greedy 2-vendor cover — the honest answer when nobody can do it alone
        need = set(range(len(sets)))
        pick = []
        for r in sorted(rows, key=lambda r: -len(set(r["covers"]) & need)):
            if not need:
                break
            gained = set(r["covers"]) & need
            if gained:
                pick.append(r["value"])
                need -= gained
        min_cover = {"groups": pick, "uncovered": sorted(need)}
    return {"ok": True, "require": list(require), "group": group,
            "total_groups": len(posts), "full_cover_count": len(full),
            "groups": rows[:limit], "min_cover": min_cover}


def analytics_versatility(index: KGIndex, over: str, base: str = "", rollup: bool = True,
                          limit: int = 10) -> dict:
    """Highest-degree node over one relation — the one question a filter can never answer."""
    over = ALIASES.get(over, over)
    B = _base_set(index, base)
    deg: dict[str, list[str]] = {}
    for sid in B:
        vs = index.subject_values.get(sid, {}).get(over, ())
        deg[sid] = sorted(index.values[over][v]["label"] for v in vs)
    pool = deg
    if rollup:
        best: dict[str, str] = {}
        for sid, vs in deg.items():
            b = index.base.get(sid, sid)
            cur = best.get(b)
            pop = float((index.attrs.get(sid) or {}).get("popularity") or 0)
            if cur is None or (len(vs), pop) > (len(deg[cur]),
                                                float((index.attrs.get(cur) or {}).get("popularity") or 0)):
                best[b] = sid
        pool = {sid: deg[sid] for sid in best.values()}
    rows = [{"dish_id": (index.attrs.get(s) or {}).get("id"),
             "base_name": index.base.get(s), "name": index.g.nodes[s].get("label"),
             "degree": len(v), "values": v,
             "popularity": (index.attrs.get(s) or {}).get("popularity"),
             "cuisine": (index.attrs.get(s) or {}).get("cuisine")}
            for s, v in pool.items()]
    rows.sort(key=lambda r: (-r["degree"], -(r["popularity"] or 0), r["base_name"] or ""))
    hist: dict[int, int] = {}
    for v in pool.values():
        hist[len(v)] = hist.get(len(v), 0) + 1
    return {"ok": True, "over": over, "base": base, "rollup": rollup, "rows": rows[:limit],
            "degree_histogram": [{"degree": d, "count": c} for d, c in sorted(hist.items())]}


ANALYTICS = {
    "coverage": analytics_coverage, "blockers": analytics_blockers,
    "substitute": analytics_substitute, "bridge": analytics_bridge,
    "one_stop": analytics_one_stop, "versatility": analytics_versatility,
}


def run_analytics(index: KGIndex, name: str, params: dict) -> dict:
    """Dispatch a canned analytics query by name. `bridge` is the only one that needs the raw
    graph, so it is the only special case."""
    fn = ANALYTICS.get(name)
    if fn is None:
        raise OntologyError("NOT_FOUND", f"No analytics endpoint named {name!r}.",
                            detail={"known": sorted(ANALYTICS)})
    p = dict(params or {})
    for k in ("tags", "via", "keep"):
        if isinstance(p.get(k), str):
            p[k] = [x.strip() for x in p[k].split(",") if x.strip()]
    if name == "bridge":
        return fn(index, index.g, **p)
    return fn(index, **p)


# ═══════════════════════════════════════════════════════════════════════════════════════
# THE TWELVE CANNED QUERIES
#
# Six slot queries and six analytics. They live in CODE, not in a data file, so a fresh clone
# has them. Every `expect` string was computed against the real 600 rows — if one of these
# starts returning something else, the derivations changed and that is a regression.
#
# They are chosen to be genuinely useful for NAVIGATION, not to flatter the data: two of them
# (gf-df-whole-team, spicy-mains-no-dairy) deliberately land on a thin result, because "the
# catalogue runs out here" is the finding a buyer most needs and the one a demo usually hides.
# ═══════════════════════════════════════════════════════════════════════════════════════

BUILTIN_QUERIES: list[dict] = [
    {"id": "vegan-nut-free-client-dinner", "title": "Vegan & nut-free client dinner",
     "kind": "slots", "icon": "🥗", "tags": ["safety", "client"], "pinned": True,
     "dsl": "diet:vegan -allergen:nuts occasion:client meal_type:dinner",
     "why": ("The single most common high-stakes brief — a client dinner with one vegan guest "
             "and one nut allergy. Getting it wrong is a career event, not an inconvenience."),
     "expect": "70 rows → 7 dishes (Chana Masala, Edamame, Eggplant Caponata, Falafel Wrap…)"},

    {"id": "gf-df-whole-team", "title": "Gluten-free + dairy-free, feeds the whole team",
     "kind": "slots", "icon": "🍽️", "tags": ["safety", "ops"],
     "dsl": "diet:gluten-free diet:dairy-free occasion:team serves>=40",
     "why": ("Two restrictions at once plus a real headcount — can ONE order cover a 40-person "
             "all-hands without a separate 'special diets' order?"),
     "expect": ("6 rows → 2 dishes (BBQ Pulled Pork, Chicken Satay). Deliberately thin — that "
                "IS the finding; ships with a populated relaxation ladder.")},

    {"id": "budget-team-lunch-25", "title": "Budget team lunch that actually feeds 25",
     "kind": "slots", "icon": "💲", "tags": ["budget", "ops"], "pinned": True,
     "dsl": "occasion:team meal_type:lunch price_pp<=15 serves>=25",
     "why": ("The weekly-lunch buyer's whole job: a per-head cap and a headcount floor at once. "
             "serves_fit floats the 25-serve trays above the 50-serve trays that waste half "
             "the order."),
     "expect": ("24 rows → 15 dishes (BBQ Pulled Pork, Buffalo Cauliflower Bites, "
                "Chicken Tinga Tacos, Cheeseburger Sliders…)")},

    {"id": "dessert-safe-for-the-room", "title": "Dessert that is safe for the whole room",
     "kind": "slots", "icon": "🍰", "tags": ["safety"],
     "dsl": "course:dessert -allergen:dairy -allergen:nuts",
     "why": ("Dessert is where allergens concentrate and where 'one person can't eat anything' "
             "is most visible at the table."),
     "expect": ("50 rows → 5 dishes (Acai Fruit Cups, Chocolate Chip Cookies, Churros, "
                "Fortune Cookies, Mango Sticky Rice). Every card carries cleared:[dairy,nuts].")},

    {"id": "spicy-mains-no-dairy", "title": "Genuinely spicy mains, no dairy",
     "kind": "slots", "icon": "🌶️", "tags": ["gap"],
     "dsl": "spice>=2 course:main -allergen:dairy",
     "why": ("Spice is asked for constantly and delivered rarely — only 40 of 600 rows are "
             "spice_level>=2. Pairing it with dairy-free is where the catalogue runs out."),
     "expect": ("30 rows → 3 dishes (Green Curry with Tofu, Kung Pao Chicken, Mapo Tofu). "
                "Add cuisine:Indian and it returns 0 — the canonical relaxation demo.")},

    {"id": "gf-breakfast", "title": "Gluten-free breakfast for a morning meeting",
     "kind": "slots", "icon": "🌅", "tags": ["safety", "breakfast"],
     "dsl": "meal_type:breakfast -allergen:gluten",
     "why": ("Breakfast catering is bread-dominated; a celiac guest eliminates most of the "
             "category, and buyers need to know that before 8 a.m."),
     "expect": ("40 of 80 breakfast rows → 4 dishes (Acai Fruit Cups, Fresh Fruit Platter, "
                "Veggie Egg Frittata, Yogurt & Granola Parfaits)")},

    {"id": "coverage-cuisine-diet", "title": "Which cuisines can't serve this crowd",
     "kind": "analytics", "icon": "🧮", "tags": ["ops", "gap"], "pinned": True,
     "endpoint": "/api/kg/analytics/coverage", "analytics": "coverage",
     "params": {"rows": "cuisine", "cols": "diet", "base": "", "min_count": 1},
     "why": ("Before shortlisting caterers, know which whole cuisines are dead ends for a "
             "restriction. Turns 'let me check 96 caterers' into 'skip these five'."),
     "expect": ("10x4 matrix, 6 zero cells: Chinese×gluten-free, and dairy-free = 0 for "
                "Breakfast, Indian, Italian, Mexican and Salads & Bowls.")},

    {"id": "blockers-team", "title": "Which single exclusion costs you the most",
     "kind": "analytics", "icon": "💥", "tags": ["ops", "risk"],
     "endpoint": "/api/kg/analytics/blockers", "analytics": "blockers",
     "params": {"base": "occasion:team", "tags": "allergen,ingredient", "limit": 10},
     "why": ("Planning a 200-person event with unknown restrictions: which single allergy would "
             "wreck the shortlist? Also tells a merchandiser which ingredient is "
             "over-concentrated."),
     "expect": ("gluten 60 · dairy 50 · rice 40 · soy 30 · nuts 30 · tomato 30 · chicken 30 · "
                "egg 30 (of 230 team dishes). Across all 600, one dairy allergy removes 38%.")},

    {"id": "substitute-pad-thai", "title": "What can replace this dish",
     "kind": "analytics", "icon": "🔄", "tags": ["ops"],
     "endpoint": "/api/kg/analytics/substitute", "analytics": "substitute",
     "params": {"dish": "Pad Thai", "keep": "dish_type,cuisine", "require": "-allergen:nuts",
                "limit": 8},
     "why": ("A restriction lands after the menu is set. The buyer doesn't want a new search — "
             "they want the nearest thing to what they already chose that clears it."),
     "expect": ("Level 1 → 0 (no nut-free Thai noodle dish exists). Level 2 → Vegetable Lo Mein "
                "(Chinese). The graph left the cuisine to keep the FORM of the dish.")},

    {"id": "bridge-thai-indian", "title": "What connects two cuisines",
     "kind": "analytics", "icon": "🌉", "tags": ["merch"],
     "endpoint": "/api/kg/analytics/bridge", "analytics": "bridge",
     "params": {"a": "cuisine:Thai", "b": "cuisine:Indian",
                "via": "ingredient,dish_type,occasion,diet", "k": 6},
     "why": ("'Asian-ish, but the client had Thai last month.' Knowing the two share curries and "
             "chicken — and diverge on coconut/peanut vs chickpea/paneer — is how you pitch the "
             "swap."),
     "expect": ("shared: ingredient:chicken · dish_type:curry · occasion:celebration,treat. "
                "Thai-only: coconut, peanut, mango, shrimp. Indian-only: chickpea, paneer, "
                "potato, tomato.")},

    {"id": "one-stop-vegan-gf", "title": "One-stop caterers for a constrained order",
     "kind": "analytics", "icon": "🏪", "tags": ["ops", "procurement"],
     "endpoint": "/api/kg/analytics/one_stop", "analytics": "one_stop",
     "params": {"require": ["diet:vegan -allergen:nuts", "diet:gluten-free -allergen:nuts"],
                "group": "caterer", "limit": 12},
     "why": ("Purchasing hates split orders — two vendors means two deliveries, two invoices, "
             "two failure points. 'Who covers ALL my constraints in one order' is a set-cover "
             "question, not a filter."),
     "expect": "86 of 96 caterers cover both requirements, 6-12 dishes each."},

    {"id": "versatility-occasion", "title": "The most versatile dish",
     "kind": "analytics", "icon": "⭐", "tags": ["merch"],
     "endpoint": "/api/kg/analytics/versatility", "analytics": "versatility",
     "params": {"over": "occasion", "base": "", "rollup": True, "limit": 10},
     "why": ("A caterer deciding what to keep permanently on the menu, and a buyer building a "
             "standing order, both want the item that works for the most different situations. "
             "Highest-degree node — the one question a filter can never answer."),
     "expect": ("Vegetable Tempura, 6 of 8 occasions — a genuine outlier: only one base dish "
                "reaches 6, the next tier is 4 (9 dishes).")},
]


# ---- saved queries -------------------------------------------------------------------

def _load_saved() -> list[dict]:
    try:
        data = _read_json(QUERIES_PATH)
        return data if isinstance(data, list) else (data or {}).get("queries") or []
    except Exception:  # noqa: BLE001
        return []


def list_queries(model_id: str, include_builtin: bool = True) -> dict:
    """Builtins merge with the file BY ID, field by field. A DELETE on a builtin writes a
    `hidden` tombstone rather than losing it, so the gesture is reversible."""
    merged: dict[str, dict] = {}
    if include_builtin:
        for q in BUILTIN_QUERIES:
            merged[q["id"]] = {**q, "builtin": True, "hidden": False,
                               "endpoint": q.get("endpoint", "/api/kg/query"),
                               "params": q.get("params", {"limit": 24, "rollup": True}),
                               "created_by": "builtin", "run_count": 0,
                               "last_run_at": None, "last_count": None}
    for q in _load_saved():
        if q.get("model") and q["model"] != model_id:
            continue
        qid = q.get("id")
        if not qid:
            continue
        merged[qid] = {**merged.get(qid, {"builtin": False}), **q}

    try:
        model = load_model(model_id)
        mh = model_hash(model)
        tags_now = {e.get("tag") for e in model.get("entity_types") or []}
        tags_now |= set(MEASURE_ATTRS) | {"text"}
        tags_now |= set(ALIASES)
    except OntologyError:
        mh, tags_now = "", set()

    out = []
    for q in merged.values():
        q = dict(q)
        q.setdefault("hidden", False)
        q.setdefault("pinned", False)
        q.setdefault("tags", [])
        q["model_id"] = model_id
        q["stale"], q["stale_reason"] = False, None
        # Re-validate on READ, not at click time: this is exactly what breaks when the user
        # edits the ontology and re-materializes, so it must be visible in the list.
        if tags_now and q.get("dsl"):
            for m in _TOKEN_RE.finditer(q["dsl"]):
                t = (m.group("tag") or "").lower()
                if t and ALIASES.get(t, t) not in tags_now:
                    q["stale"] = True
                    q["stale_reason"] = f"slot {t!r} is no longer in the model"
                    break
        # Analytics queries carry no `dsl` -- their subject lives in `params` (rows/cols/over/
        # via/tags/keep/group) plus nested DSL strings (base/require). Without this they read
        # "ok" on an ontology that cannot answer them and fail SILENTLY at click time: an empty
        # coverage matrix, a most-versatile dish of degree 0, a blockers base that quietly
        # widened to the whole catalogue. Same re-validation, same moment.
        if tags_now and not q["stale"] and isinstance(q.get("params"), dict):
            _pm = q["params"]
            _bad = None
            for _k in ("rows", "cols", "over", "group"):
                _v = str(_pm.get(_k) or "").strip().lower()
                if _v and ALIASES.get(_v, _v) not in tags_now:
                    _bad = (_v, _k)
                    break
            if _bad is None:
                for _k in ("tags", "via", "keep"):
                    _raw = _pm.get(_k)
                    _items = (_raw if isinstance(_raw, (list, tuple))
                              else str(_raw or "").split(","))
                    for _t in [str(x).strip().lower() for x in _items if str(x).strip()]:
                        if ALIASES.get(_t, _t) not in tags_now:
                            _bad = (_t, _k)
                            break
                    if _bad:
                        break
            if _bad is None:
                for _k in ("base", "require", "a", "b"):
                    _raw = _pm.get(_k)
                    for _src in (_raw if isinstance(_raw, (list, tuple)) else [_raw]):
                        _src = str(_src or "").strip()
                        if not _src:
                            continue
                        for _m in _TOKEN_RE.finditer(_src):
                            _t = (_m.group("tag") or "").lower()
                            if _t and ALIASES.get(_t, _t) not in tags_now:
                                _bad = (_t, _k)
                                break
                        if _bad:
                            break
                    if _bad:
                        break
            if _bad:
                q["stale"] = True
                q["stale_reason"] = (f"{_bad[0]!r} ({_bad[1]}) is no longer in the model")
        if mh and q.get("model_hash") and q["model_hash"] != mh and not q["stale"]:
            q["stale_reason"] = "the ontology changed since this query was saved"
        out.append(q)

    out = [q for q in out if not q.get("hidden")]
    out.sort(key=lambda q: (not q.get("pinned"), q.get("builtin") is not True, q.get("title", "")))
    all_tags = sorted({t for q in out for t in q.get("tags") or []})
    return {"ok": True, "model_id": model_id, "tags": all_tags, "queries": out}


def save_query(q: dict, index: KGIndex, *, now: str) -> dict:
    """You cannot save an already-broken rule: it must parse cleanly against the LIVE index
    before it earns a place in the list."""
    title = (q.get("title") or "").strip()
    dsl = (q.get("dsl") or "").strip()
    if not title:
        raise OntologyError("INVALID_QUERY", "A saved query needs a title.")
    warning = None
    if dsl:
        parsed = parse(dsl, index)
        if parsed["blocked"]:
            raise OntologyError("INVALID_QUERY",
                                "That query is blocked on an unresolved safety exclusion: "
                                "it names an allergen this ontology cannot verify.",
                                detail={"warnings": parsed["warnings"]})
        bad_tags = [w.get("tag") for w in parsed["warnings"]
                    if w.get("code") == "unknown_tag" and w.get("tag")]
        if bad_tags:
            named = ", ".join(f'"{t}"' for t in dict.fromkeys(bad_tags))
            raise OntologyError(
                "INVALID_QUERY",
                f"This ontology has no slot called {named}. Add that entity type to the model, "
                f"or use one of the slots the query bar offers.",
                detail={"warnings": parsed["warnings"], "unknown_tags": bad_tags})
        res = execute(parsed, index, limit=1, subgraph=False, facets=False)
        last_count = res["counts"]["distinct"]
        if last_count == 0:
            warning = "empty_at_save"
    elif not q.get("endpoint"):
        raise OntologyError("INVALID_QUERY", "A saved query needs `dsl` or `endpoint`.")
    else:
        last_count = None

    existing = {x["id"] for x in list_queries(index.model_id)["queries"]}
    qid, n = _slug(title), 1
    while qid in existing:
        n += 1
        qid = f"{_slug(title)}-{n}"
    row = {"id": qid, "model": index.model_id, "title": title, "dsl": dsl,
           "kind": "analytics" if q.get("endpoint") else "slots",
           "endpoint": q.get("endpoint") or "/api/kg/query",
           "params": q.get("params") or {"limit": 24, "rollup": True},
           "why": q.get("why", ""), "icon": q.get("icon", "🔎"),
           "tags": list(q.get("tags") or []), "pinned": bool(q.get("pinned")),
           "hidden": False, "builtin": False, "model_hash": model_hash(index.model),
           "created_at": now, "created_by": "user", "run_count": 0,
           "last_run_at": None, "last_count": last_count}
    saved = _load_saved()
    saved.append(row)
    _write_json_atomic(QUERIES_PATH, saved)
    if warning:
        row["warning"] = warning
    return row


def patch_query(qid: str, patch: dict, *, now: str) -> dict:
    saved = _load_saved()
    allowed = {"title", "dsl", "why", "icon", "tags", "pinned", "hidden",
               "params", "last_count", "run_count", "last_run_at"}
    for row in saved:
        if row.get("id") == qid:
            row.update({k: v for k, v in patch.items() if k in allowed})
            row["updated_at"] = now
            _write_json_atomic(QUERIES_PATH, saved)
            return row
    builtin = next((q for q in BUILTIN_QUERIES if q["id"] == qid), None)
    if builtin is None:
        raise OntologyError("NOT_FOUND", f"No saved query {qid!r}.", detail={"id": qid})
    # An override of a builtin is stored as a sparse patch row, so a later change to the
    # constant still reaches the user for every field they did not touch.
    row = {"id": qid, **{k: v for k, v in patch.items() if k in allowed}, "updated_at": now}
    saved.append(row)
    _write_json_atomic(QUERIES_PATH, saved)
    return {**builtin, **row}


def delete_query(qid: str) -> bool:
    saved = _load_saved()
    n = len(saved)
    saved = [r for r in saved if r.get("id") != qid]
    if any(q["id"] == qid for q in BUILTIN_QUERIES):
        saved.append({"id": qid, "hidden": True})   # reversible tombstone
    _write_json_atomic(QUERIES_PATH, saved)
    return len(saved) != n or any(q["id"] == qid for q in BUILTIN_QUERIES)


def run_saved_query(index: KGIndex, qid: str, **kwargs) -> dict:
    """Execute a canned query by id, routing slot queries and analytics queries alike."""
    row = next((q for q in list_queries(index.model_id)["queries"] if q["id"] == qid), None)
    if row is None:
        raise OntologyError("NOT_FOUND", f"No saved query {qid!r}.", detail={"id": qid})
    if row.get("kind") == "analytics" or row.get("endpoint", "").startswith("/api/kg/analytics"):
        name = row.get("analytics") or row["endpoint"].rsplit("/", 1)[-1]
        return {"ok": True, "query": row, "kind": "analytics",
                "result": run_analytics(index, name, row.get("params") or {})}
    params = {**{"limit": 24, "rollup": True}, **(row.get("params") or {}), **kwargs}
    params = {k: v for k, v in params.items()
              if k in ("limit", "offset", "rollup", "facets", "subgraph")}
    return {"ok": True, "query": row, "kind": "slots",
            "result": execute(parse(row["dsl"], index), index, **params)}
