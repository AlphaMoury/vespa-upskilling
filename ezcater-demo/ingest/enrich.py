"""
The shared enrichment stage — the food-ontology "backend data enrichment" use case.

Every MenuItem from every adapter passes through enrich(). It fills the denormalized
fields Vespa filters on (ingredients, allergens, dietary, spice_level, flavor, occasion).

Three layers, most-to-least trusted, all optional beyond the first:
  1. taxonomy  (deterministic curated ontology)  -- ALWAYS runs, the reliable core
  2. Open Food Facts (live corroboration)         -- only if OFF_LIVE=1
  3. LLM extraction (OpenAI)                       -- only if index_llm_enabled()

The LLM/OFF work is memoized by ingredient-set hash (cache.py), so re-indexing and
replays skip it. Allergen exclusion is treated as safety-critical: a *declared* diet
(vegan/gluten-free) removes the contradicting allergen, never the reverse.
"""

from __future__ import annotations

from .menu_item import MenuItem
from . import taxonomy, off, cache, llm
from .graph import get_graph
from .config import index_llm_enabled

# normalize the many ways sources state a diet
_DIET_NORM = {
    "vegan": "vegan", "plant-based": "vegan", "plant based": "vegan",
    "vegetarian": "vegetarian", "veggie": "vegetarian", "lacto-vegetarian": "vegetarian",
    "gluten-free": "gluten-free", "gluten free": "gluten-free", "glutenfree": "gluten-free",
    "dairy-free": "dairy-free", "dairy free": "dairy-free", "non-dairy": "dairy-free",
    "halal": "halal", "kosher": "kosher", "pescatarian": "pescatarian", "keto": "keto",
    "paleo": "paleo", "nut-free": "nut-free", "nut free": "nut-free",
}

_LLM_SYS = (
    "You extract structured food facts from a catering menu item. Return ONLY JSON with keys: "
    "ingredients (array of lowercase base ingredients), "
    "allergens (array from: gluten,dairy,eggs,nuts,peanuts,soy,shellfish,fish,sesame), "
    "dietary (array from: vegan,vegetarian,gluten-free,dairy-free,pescatarian), "
    "spice_level (integer 0-3). "
    "Only list what is clearly implied by the text. Do not invent. Prefer empty arrays over guessing."
)


def _norm_diet(tags) -> list[str]:
    out = []
    for t in tags or []:
        key = str(t).strip().lower()
        v = _DIET_NORM.get(key)
        if v and v not in out:
            out.append(v)
    return out


def _llm_extract(item: MenuItem) -> dict:
    """Cached, optional LLM enrichment keyed by ingredient-set hash. {} if disabled/miss."""
    if not index_llm_enabled():
        return {}
    ckey = f"llm:{item.ingredient_key()}"
    hit = cache.get_json(ckey)
    if hit is not None:
        return hit
    user = f"Item: {item.name}\nDescription: {item.description or ''}\nCuisine: {item.cuisine or ''}"
    if item.ingredients_raw:
        user += "\nStated ingredients: " + ", ".join(item.ingredients_raw[:40])
    data = llm.chat_json(_LLM_SYS, user) or {}
    clean = {
        "ingredients": [str(x).lower().strip() for x in data.get("ingredients", []) if str(x).strip()][:40],
        "allergens": [a for a in data.get("allergens", []) if a in taxonomy.ALLERGENS],
        "dietary": _norm_diet(data.get("dietary", [])),
        "spice_level": int(data.get("spice_level") or 0) if str(data.get("spice_level", "")).isdigit() else 0,
    }
    cache.set_json(ckey, clean)
    return clean


def _infer_dietary(ingredients: list[str], allergens: set[str], declared: list[str],
                   conflicts: set[str]) -> list[str]:
    """`conflicts` = diets the ONTOLOGY GRAPH says an ingredient violates (vegan/vegetarian).
    Positive labels are only inferred when we have ingredient signal AND nothing conflicts."""
    ings = bool(ingredients)
    out = list(declared)
    if ings:
        if "vegan" not in conflicts and "vegan" not in out:
            out.append("vegan")
        if "vegetarian" not in conflicts and "vegetarian" not in out:
            out.append("vegetarian")
    if "vegan" in out and "vegetarian" not in out:
        out.append("vegetarian")
    if "gluten" not in allergens and ings and "gluten-free" not in out:
        out.append("gluten-free")
    if "dairy" not in allergens and ings and "dairy-free" not in out:
        out.append("dairy-free")
    return list(dict.fromkeys(out))


def _occasion(item: MenuItem, ingredients: list[str]) -> list[str]:
    t = f"{item.name} {item.description or ''}".lower()
    course = (item.course or "").lower()
    occ: set[str] = set()
    if course == "breakfast" or (item.cuisine or "").lower() == "breakfast" or any(
            k in t for k in ["breakfast", "bagel", "pancake", "omelet", "frittata", "parfait", "brunch"]):
        occ |= {"breakfast", "morning"}
    if course == "dessert" or any(k in t for k in ["cookie", "cake", "brownie", "tiramisu", "baklava", "churro", "mochi", "dessert"]):
        occ |= {"celebration", "treat"}
    if any(k in t for k in ["quinoa", "kale", "buddha", "salad", "healthy", "wholesome", "nutritious", "light", "grain bowl", "power"]):
        occ |= {"healthy", "light"}
    if any(k in t for k in ["mac", "bbq", "pulled pork", "fried", "cheeseburger", "pizza", "comfort", "nachos", "wings"]):
        occ |= {"comfort", "team"}
    if (item.price_pp or 0) >= 24 or any(k in t for k in ["premium", "executive", "deluxe", "chef", "signature"]):
        occ |= {"client", "impressive", "dinner"}
    if not occ:
        occ |= {"lunch", "team"}
    return sorted(occ)


def enrich(item: MenuItem) -> MenuItem:
    """Fill the ontology fields on a MenuItem in place; returns it for chaining."""
    text = f"{item.name} {item.description or ''}"

    # 1) ingredients: raw + text-extracted + (optional) LLM
    ing = {i.lower().strip() for i in (item.ingredients_raw or []) if i.strip()}
    ing |= set(taxonomy.extract_ingredients(text))
    lx = _llm_extract(item)
    ing |= set(lx.get("ingredients", []))
    ingredients = sorted(i for i in ing if i)

    # 2) allergens + diet-conflicts from the ONTOLOGY GRAPH (grows via LLM at ingest), + OFF + LLM
    gout = get_graph().enrich(ingredients, grow=index_llm_enabled())
    allergens = set(gout["allergens"]) | set(lx.get("allergens", []))
    conflicts = set(gout["diet_conflicts"])
    if off.live_enabled():
        for tok in ingredients[:12]:  # cap live calls per item
            allergens |= off.allergens_for_ingredient(tok)

    # 3) declared diet is authoritative: it REMOVES contradicting allergens (safety)
    declared = _norm_diet(item.dietary_declared) + _norm_diet(lx.get("dietary", []))
    if "gluten-free" in declared:
        allergens.discard("gluten")
    if "vegan" in declared or "dairy-free" in declared:
        allergens.discard("dairy")
    if "vegan" in declared:
        allergens.discard("eggs")
    if "nut-free" in declared:
        allergens.discard("nuts"); allergens.discard("peanuts")

    # 4) dietary inference (graph conflicts + allergen-implied), then a safety guard that blocks
    #    INFERRED (not declared) vegan/vegetarian when the name/description names an animal.
    if allergens & {"dairy", "eggs"}:
        conflicts.add("vegan")
    if allergens & {"fish", "shellfish"}:
        conflicts |= {"vegan", "vegetarian"}
    dietary = _infer_dietary(ingredients, allergens, declared, conflicts)
    flesh, animal = taxonomy.animal_in_text(text)
    if flesh:
        dietary = [d for d in dietary if not (d in ("vegan", "vegetarian") and d not in declared)]
    if animal and "vegan" not in declared:
        dietary = [d for d in dietary if d != "vegan"]

    # 5) spice / flavor / occasion
    spice = max(taxonomy.spice_for(text), int(lx.get("spice_level") or 0))
    if (item.course or "").lower() == "dessert":
        flavor = "sweet"
    elif spice >= 2:
        flavor = "spicy"
    elif any(k in text.lower() for k in ["salad", "fresh", "citrus", "lemon", "bowl"]):
        flavor = "fresh"
    else:
        flavor = "savory"

    item.ingredients = ingredients
    item.allergens = sorted(allergens)
    item.dietary = dietary
    item.spice_level = spice
    item.flavor = flavor
    item.occasion = _occasion(item, ingredients)
    # PDF/LLM sources can carry <1.0 confidence set by the adapter; leave as-is otherwise
    return item
