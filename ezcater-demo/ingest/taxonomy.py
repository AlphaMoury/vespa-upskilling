"""
The food-ontology spine: a curated ingredient -> {allergens, animal-class} taxonomy.

This is the deterministic core of enrichment — a small FoodOn-style map you can seed and
audit. It's the reliable ground truth; the live Open Food Facts lookup (off.py) and the
optional LLM (llm.py) *corroborate and extend* it, they don't replace it.

The 14 EU/FDA major allergens are the target vocabulary; we cover the common ones.
"""

from __future__ import annotations

import re

# ---- major allergens we track (subset of the 14 regulated) ----
ALLERGENS = ["gluten", "dairy", "eggs", "nuts", "peanuts", "soy", "shellfish", "fish", "sesame"]

# The LLM's substring traps: names that LOOK like an allergen but aren't, plus "may contain"
# guesses asserted as fact. Allergen labels are safety-critical, so the deterministic layer
# VETOES these — a hallucinated edge never reaches a dish's allergens[]. Small and auditable:
# every entry is a claim a human reviewed and rejected.
ALLERGEN_FALSE_POSITIVES: set[tuple[str, str]] = {
    ("shells", "shellfish"),              # pasta shells, not a mollusc
    ("nutmeg", "nuts"),                   # a spice; the substring "nut" is the trap
    ("shrimp", "fish"),                   # shrimp is shellfish, not fish
    ("jumbo shrimp", "fish"),
    # "may contain nuts" asserted as "contains nuts" — this wrongly empties a nut-free dessert search
    ("cookie", "nuts"),
    ("brownies", "nuts"),
    ("chocolate chips", "nuts"),
    ("dark chocolate", "nuts"),
    ("german chocolate", "nuts"),
    ("german chocolate square", "nuts"),
    ("granola-type cereal", "nuts"),
}

# ingredient token -> allergens it implies
INGREDIENT_ALLERGENS: dict[str, set[str]] = {
    # gluten
    "bread": {"gluten"}, "pita": {"gluten"}, "naan": {"gluten"}, "pasta": {"gluten"},
    "penne": {"gluten"}, "noodle": {"gluten"}, "noodles": {"gluten"}, "flour": {"gluten"},
    "tortilla": {"gluten"}, "bun": {"gluten"}, "brioche": {"gluten"}, "phyllo": {"gluten"},
    "dough": {"gluten"}, "crouton": {"gluten"}, "wrap": {"gluten"}, "bagel": {"gluten"},
    "roll": {"gluten"}, "cracker": {"gluten"}, "ladyfinger": {"gluten"}, "couscous": {"gluten"},
    "barley": {"gluten"}, "wheat": {"gluten"}, "farro": {"gluten"}, "breadcrumb": {"gluten"},
    "soy sauce": {"gluten", "soy"},
    # dairy
    "cheese": {"dairy"}, "cream": {"dairy"}, "mozzarella": {"dairy"}, "feta": {"dairy"},
    "yogurt": {"dairy"}, "butter": {"dairy"}, "mascarpone": {"dairy"}, "paneer": {"dairy"},
    "milk": {"dairy"}, "parmesan": {"dairy"}, "ricotta": {"dairy"}, "goat cheese": {"dairy"},
    "cheddar": {"dairy"}, "icing": {"dairy"}, "ghee": {"dairy"},
    # eggs
    "egg": {"eggs"}, "eggs": {"eggs"}, "mayo": {"eggs"}, "mayonnaise": {"eggs"}, "aioli": {"eggs"},
    # tree nuts / peanuts
    "almond": {"nuts"}, "walnut": {"nuts"}, "pecan": {"nuts"}, "cashew": {"nuts"},
    "pistachio": {"nuts"}, "hazelnut": {"nuts"}, "pine nut": {"nuts"},
    "peanut": {"peanuts"}, "peanuts": {"peanuts"},
    # soy
    "tofu": {"soy"}, "soy": {"soy"}, "edamame": {"soy"}, "teriyaki": {"soy"}, "miso": {"soy"}, "tempeh": {"soy"},
    # seafood
    "shrimp": {"shellfish"}, "prawn": {"shellfish"}, "crab": {"shellfish"}, "lobster": {"shellfish"},
    "salmon": {"fish"}, "tuna": {"fish"}, "fish": {"fish"}, "anchovy": {"fish"},
    # sesame
    "sesame": {"sesame"}, "tahini": {"sesame"}, "hummus": {"sesame"},
}

# animal classes for dietary inference
MEAT = {"chicken", "pork", "beef", "bacon", "sausage", "carnitas", "carne", "turkey", "lamb", "ham",
        "steak", "meatball", "chorizo", "pepperoni", "salami", "prosciutto", "brisket", "veal", "duck",
        "pastrami", "pancetta", "gelatin"}
SEAFOOD = {"shrimp", "prawn", "crab", "lobster", "salmon", "tuna", "fish", "anchovy", "sushi", "scallop",
           "clam", "mussel", "oyster", "cod", "halibut", "squid", "calamari"}
ANIMAL_NONMEAT = {"cheese", "cream", "milk", "butter", "yogurt", "egg", "eggs", "honey", "mozzarella",
                  "feta", "parmesan", "paneer", "mascarpone", "ricotta", "cheddar", "ghee", "mayo", "mayonnaise"}


def animal_in_text(text: str) -> tuple[bool, bool]:
    """Scan free text (name+desc) for animal words. Returns (has_meat_or_seafood, has_animal_product).
    Used to BLOCK over-eager vegan/vegetarian inference on messy real data (e.g. 'Chili Con Carne')."""
    v = _word_variants((text or "").lower())
    flesh = bool(v & MEAT) or bool(v & SEAFOOD)
    animal = flesh or bool(v & ANIMAL_NONMEAT)
    return flesh, animal

# ingredient vocabulary we recognize when extracting from free text
INGREDIENT_VOCAB = sorted(set(INGREDIENT_ALLERGENS) | MEAT | SEAFOOD | ANIMAL_NONMEAT | {
    "rice", "chickpea", "avocado", "tomato", "basil", "eggplant", "olive", "quinoa", "kale",
    "bean", "lentil", "mango", "coconut", "potato", "spinach", "cucumber", "cauliflower",
    "granola", "berry", "apple", "onion", "garlic", "pepper", "mushroom", "carrot", "corn",
    "lettuce", "guacamole", "salsa", "curry", "hummus", "falafel", "pesto",
})

SPICE = {"mapo": 3, "kung pao": 2, "gochujang": 2, "buffalo": 2, "curry": 2, "sriracha": 2,
         "jalapeno": 2, "jalapeño": 2, "chipotle": 1, "tikka": 1, "masala": 1, "satay": 1,
         "kimchi": 1, "tinga": 1, "shawarma": 1, "harissa": 2, "cajun": 1}


def extract_ingredients(text: str) -> list[str]:
    """Deterministic ingredient extraction: vocabulary tokens present in the text.
    Single-word tokens match on whole words (with plural handling: 'walnuts'->'walnut');
    multi-word tokens ('goat cheese', 'pine nut') match as substrings."""
    lower = (text or "").lower()
    variants = _word_variants(lower)
    t = " " + re.sub(r"[^a-z0-9 ]", " ", lower) + " "
    found = []
    for token in INGREDIENT_VOCAB:
        if " " in token:
            if f" {token} " in t:
                found.append(token)
        elif token in variants:
            found.append(token)
    return sorted(set(found))


def _word_variants(ing: str) -> set[str]:
    """Whole words plus light singularization (cashews->cashew, berries->berry) so
    plurals still match, WITHOUT letting 'eggplant' match 'egg' (no substring)."""
    v: set[str] = set()
    for w in re.findall(r"[a-z]+", ing):
        v.add(w)
        if w.endswith("ies") and len(w) > 4:
            v.add(w[:-3] + "y")
        elif w.endswith("es") and len(w) > 4:
            v.add(w[:-1]); v.add(w[:-2])
        elif w.endswith("s") and len(w) > 3:
            v.add(w[:-1])
    return v


def allergens_for(ingredients: list[str]) -> set[str]:
    """Map ingredients -> allergens. Single-word tokens match on WHOLE WORDS (with light
    plural handling) so that 'eggplant' does not trigger 'egg' and 'shellfish' does not
    trigger 'fish'. Multi-word tokens ('soy sauce', 'goat cheese') match as substrings."""
    out: set[str] = set()
    for ing in ingredients:
        ing = ing.lower().strip()
        if ing in INGREDIENT_ALLERGENS:
            out |= INGREDIENT_ALLERGENS[ing]
            continue
        words = _word_variants(ing)
        for token, al in INGREDIENT_ALLERGENS.items():
            if " " in token:
                if token in ing:
                    out |= al
            elif token in words:
                out |= al
    return out


def spice_for(text: str) -> int:
    t = (text or "").lower()
    spice = 0
    for k, v in SPICE.items():
        if k in t:
            spice = max(spice, v)
    if "spicy" in t or "chili" in t or "chilli" in t or "hot sauce" in t:
        spice = max(spice, 2)
    return spice
