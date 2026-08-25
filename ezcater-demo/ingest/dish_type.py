"""
Deterministic dish TYPE and MEAL TYPE derivation — the two vocabularies that
`data/dishes.jsonl` implies but never states.

Why this file exists at all: the Knowledge Graph needs `dish_type` and `meal_type` as
first-class node kinds, but the corpus only carries name/description/course/occasion.
Every other graph axis (cuisine, course, flavor, allergens…) is a literal field read;
these two have to be *computed*, so they get their own pure module with no I/O, no
LLM and no network. Rebuilding the graph must never depend on a model being up.

Two levels of dish type:

    dish_type  --IS_A-->  dish_type_group        (curry -> soup_and_stew)

The leaf answers "what IS this dish", the group answers "what FAMILY is it in", which
is what makes rollup queries ("show me every handheld under $15") possible at all.

`web/src/App.jsx` already ships a 53-entry `FOOD_RULES` table doing keyword→category
matching, and it is ported here verbatim (see FOOD_RULES / food_cat below) — but only
for what it is actually good at: picking a decorative JPEG. It is NOT the taxonomy.
Its regexes use a leading-only word boundary (`\\b(egg|...)`), so *Eggplant Caponata*
types as `egg` and *Vegetable Tempura* types as `shrimp`. Wrong photos are forgivable;
wrong graph edges are not. DISH_TYPE_RULES below is the corrected taxonomy: same
ordering philosophy (dish FORM beats preparation beats protein beats raw ingredient),
but the boundary wraps the WHOLE alternation and the vocabulary is a real two-level tree.

Consumed by `server/ontology.py` (materializer) and `ingest/vocab.py` (projection).
"""

from __future__ import annotations

import re
from typing import Iterable

# --------------------------------------------------------------------------------------
# 1. THE TAXONOMY
# --------------------------------------------------------------------------------------

# 8 families x 42 leaves. The family layer is not decoration: it is the rollup axis for
# the query bar ("handheld", "sweet") and the only way a 600-row corpus spread over 42
# leaves still has buckets big enough to browse.
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

PARENT_OF: dict[str, str] = {leaf: parent for parent, leaves in DISH_TYPE_TREE.items() for leaf in leaves}
# "other" is its own family so `dish_type_group()` is total — an unmatched dish must never
# silently land in a real family and pollute a rollup.
PARENT_OF["other"] = "other"

DISH_TYPE_LEAVES: list[str] = [leaf for leaves in DISH_TYPE_TREE.values() for leaf in leaves] + ["other"]  # 43
DISH_TYPE_GROUPS: list[str] = list(DISH_TYPE_TREE) + ["other"]                                            # 9

# --------------------------------------------------------------------------------------
# 2. THE ORDERED RULE LIST — order IS the semantics
# --------------------------------------------------------------------------------------
# Read top to bottom: dish FORM (sushi, pizza, wrap) -> preparation (fried, skewered) ->
# protein (poultry, beef) -> raw ingredient (vegetable, potato) -> sweets -> drinks.
# "Chicken Tikka Masala" must be a `curry`, not `poultry`: what you order is the curry.
# "Falafel Wrap" must be a `wrap`, not a `fritter`: what arrives is a wrap.
# Reordering this list silently changes thousands of graph edges. Don't.
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

# THE BOUNDARY FIX. App.jsx compiles `\b(alt1|alt2)` — a LEADING boundary only, so `egg`
# matches inside "eggplant" and `shrimp|prawn|tempura` claims "Vegetable Tempura".
# Wrapping the whole alternation in a lookbehind AND a lookahead is the entire difference
# between a photo-picker and a taxonomy.
_COMPILED: list[tuple[str, re.Pattern[str]]] = [
    (leaf, re.compile(r"(?<![a-z])(?:" + pattern + r")(?![a-z])", re.I))
    for leaf, pattern in DISH_TYPE_RULES
]

# A bare "bowl" is a dish FORM the rule list cannot see: `salad` would eat "Veggie Burrito
# Bowl" and "Chicken Teriyaki Bowl" alike. Bowls get routed by their filling instead.
_BOWL_RE = re.compile(r"\bbowls?\b", re.I)
_BOWL_RICE_RE = re.compile(r"rice|teriyaki|poke|donburi|sushi", re.I)
_BOWL_NOODLE_RE = re.compile(r"noodle|ramen|pho|udon", re.I)
_BOWL_SOUP_RE = re.compile(r"soup|broth|stew|chowder", re.I)

# Leaves eligible when course == "dessert". Without this guard *Gulab Jamun* — described
# as "milk-dough DUMPLINGS soaked in syrup" — types as `dumpling`, and a dessert shows up
# in the small-plates family. The course field is ground truth; trust it over the text.
_SWEET_LEAVES: frozenset[str] = frozenset(DISH_TYPE_TREE["sweet"])


def dish_type(name: str, description: str = "", course: str = "") -> str:
    """Leaf dish type for one dish. Pure, deterministic, first-match-wins.

    `course` is optional but load-bearing: pass it whenever you have it, or desserts
    described with savoury nouns will mistype. Returns a member of DISH_TYPE_LEAVES.
    """
    nm = (name or "").lower()
    desc = (description or "").lower()
    crs = (course or "").lower()
    both = f"{nm} {desc}"

    is_dessert = crs == "dessert"
    rules = [(leaf, rx) for leaf, rx in _COMPILED if leaf in _SWEET_LEAVES] if is_dessert else _COMPILED

    # BOWL ROUTER — short-circuits the rule list entirely (savoury bowls only).
    if not is_dessert and _BOWL_RE.search(nm):
        if "burrito" in nm:
            return "burrito"
        if _BOWL_RICE_RE.search(both):
            return "rice"
        if _BOWL_NOODLE_RE.search(both):
            return "noodles"
        if _BOWL_SOUP_RE.search(both):
            return "soup"
        return "grain_bowl"

    # NAME first: the name is the dish's own claim about what it is. Only fall through to
    # the description when the name says nothing — descriptions mention garnishes, sides
    # and cooking methods that would otherwise outrank the actual dish.
    for leaf, rx in rules:
        if rx.search(nm):
            return leaf
    for leaf, rx in rules:
        if rx.search(both):
            return leaf

    return "custard" if is_dessert else "other"


def dish_type_group(leaf: str) -> str:
    """Family for a leaf. Total: anything unknown collapses to "other"."""
    return PARENT_OF.get(leaf, "other")


# Back-compat / convenience aliases. `dish_type_for` mirrors the (name, description)
# call shape used by the ad-hoc corpus scripts; `course` stays optional.
def dish_type_for(name: str, description: str = "", course: str = "") -> str:
    return dish_type(name, description, course)


def dish_type_group_for(name: str, description: str = "", course: str = "") -> str:
    return dish_type_group(dish_type(name, description, course))


def classify_all(rows: Iterable[dict]) -> dict[str, str]:
    """dish id -> leaf, for a stream of dishes.jsonl `fields` dicts."""
    out: dict[str, str] = {}
    for f in rows:
        out[str(f.get("id", ""))] = dish_type(
            f.get("name", ""), f.get("description", ""), f.get("course", "")
        )
    return out


def coverage(rows: Iterable[dict]) -> dict:
    """Histogram + unmatched ids. This is the report that tells you a rule regressed:
    a rising `other` count or a leaf that stops firing is a broken regex, not a data drift."""
    leaf_counts: dict[str, int] = {}
    group_counts: dict[str, int] = {}
    unmatched: list[str] = []
    for f in rows:
        leaf = dish_type(f.get("name", ""), f.get("description", ""), f.get("course", ""))
        grp = dish_type_group(leaf)
        leaf_counts[leaf] = leaf_counts.get(leaf, 0) + 1
        group_counts[grp] = group_counts.get(grp, 0) + 1
        if leaf == "other":
            unmatched.append(str(f.get("id", "")))
    return {
        "leaf_counts": dict(sorted(leaf_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "group_counts": dict(sorted(group_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "unmatched": unmatched,
        "leaves_used": len(leaf_counts),
        "leaves_declared": len(DISH_TYPE_LEAVES),
        "groups_used": len(group_counts),
        "groups_declared": len(DISH_TYPE_GROUPS),
    }


# --------------------------------------------------------------------------------------
# 3. MEAL TYPE
# --------------------------------------------------------------------------------------
# `fields.occasion` mixes two different questions: WHEN is this eaten (breakfast, lunch,
# dinner) and WHAT is the situation (client, team, celebration). Leaving them fused means
# "dinner" and "impressive" sit on the same axis, which no buyer thinks that way about.
# We split the four meal-time values out into their own vocabulary and DERIVE the rest,
# because two thirds of the corpus states no meal time at all.
MEAL_TYPES: list[str] = ["breakfast", "brunch", "lunch", "dinner", "snack", "dessert"]

# These leave `occasion` and become `meal_type`. `morning` is dropped as a `breakfast`
# synonym — two node ids for one concept is a bug the user sees as duplicate facets.
MEAL_TIME_OCCASIONS: frozenset[str] = frozenset({"breakfast", "morning", "lunch", "dinner"})

# Breakfast is the one meal identifiable from the food itself; nobody serves a frittata at
# 7pm. Keyword evidence backstops the (sparse) occasion/cuisine signals.
BREAKFAST_RE = re.compile(
    r"\b(breakfast|brunch|bagel|pancake|waffle|omelet|omelette|frittata|parfait|"
    r"granola|croissant|muffin|scone|danish|toast|oatmeal|benedict|hash brown|"
    r"cinnamon roll|coffee|espresso|latte|yogurt|acai|smoothie)\b", re.I)

# Forms you eat standing up with one hand. Combined with course in ("appetizer","dessert")
# this is what "snack" actually means for catering — not a time of day, a serving mode.
SNACKABLE: frozenset[str] = frozenset({
    "dip", "platter", "fried_bite", "skewer", "cookie",
    "fruit", "chocolate", "dumpling", "fritter",
})


def meal_type(fields: dict, dtype: str) -> list[str]:
    """Meal times a dish is plausible for. Multi-valued on purpose: a curry is genuinely
    both lunch and dinner, and forcing a single value would make half the corpus wrong.

    `dtype` is the leaf from dish_type(); pass it in so callers that already computed it
    don't pay for it twice (the materializer does one pass over 600 docs).
    """
    occ = set(fields.get("occasion") or [])
    course = (fields.get("course") or "").lower()
    cuisine = fields.get("cuisine") or ""
    try:
        pp = float(fields.get("price_pp") or 0)
    except (TypeError, ValueError):
        pp = 0.0
    text = f"{fields.get('name', '')} {fields.get('description', '')}"
    out: set[str] = set()

    is_bk = (
        cuisine == "Breakfast"
        or course == "breakfast"
        or "breakfast" in occ
        or "morning" in occ
        or bool(BREAKFAST_RE.search(text))
    )

    if course == "dessert":                                                     # R1
        out.add("dessert")
    if is_bk:                                                                   # R2
        out.add("breakfast")
    # R3 — brunch is breakfast food at a price point or an occasion that implies sitting
    # down: $14/head of pastries is a catered brunch, $6/head is the office coffee run.
    if is_bk and (pp >= 14 or "brunch" in text.lower()
                  or (occ & {"celebration", "impressive", "client"})):
        out.add("brunch")
    if "lunch" in occ:                                                          # R4
        out.add("lunch")
    if "dinner" in occ:                                                         # R5
        out.add("dinner")
    if course in ("appetizer", "dessert") and dtype in SNACKABLE:               # R6
        out.add("snack")
    # R7 — a main with no stated meal time defaults to lunch; only an expensive or
    # client-facing main earns dinner too. Without this, 200 mains have no meal_type at
    # all and the whole facet reads as broken.
    if not (out & {"lunch", "dinner"}) and course == "main" and not is_bk:
        out.add("lunch")
        if pp >= 20 or (occ & {"impressive", "client"}):
            out.add("dinner")
    if not out:                                                                 # R8
        out.add("lunch")
    return sorted(out)


def meal_type_for(fields: dict) -> tuple[str, ...]:
    """`meal_type` for callers that have only the raw row — derives `dtype` itself and
    returns a hashable tuple so combinations can be counted directly."""
    dtype = dish_type(fields.get("name", ""), fields.get("description", ""), fields.get("course", ""))
    return tuple(meal_type(fields, dtype))


def occasions(fields: dict) -> list[str]:
    """`fields.occasion` minus the meal-time values, which now live in `meal_type`.
    The stored field is NEVER rewritten — the Vespa `dish` schema and /api/search both
    key off it. Only this graph projection splits it."""
    return sorted(o for o in (fields.get("occasion") or []) if o not in MEAL_TIME_OCCASIONS)


# --------------------------------------------------------------------------------------
# 4. FOOD_RULES — verbatim port of web/src/App.jsx, for PHOTOS ONLY
# --------------------------------------------------------------------------------------
# 53 entries, original order preserved. This is deliberately NOT the taxonomy: it exists
# so Python can compute the same `web/public/food/<cat>.jpg` key the React card computes,
# and nothing else. Keeping it byte-compatible (leading `\b(` boundary and all) is the
# point — if it diverges, server-rendered payloads and client-rendered cards show
# different photos for the same dish.
FOOD_RULES: list[tuple[str, str]] = [
    ("sushi", "sushi|sashimi|maki|nigiri|poke"),
    ("burrito", "burrito|quesadilla|enchilada|chimichanga"),
    ("taco", "taco"),
    ("pizza", "pizza|margherita|calzone"),
    ("pasta", "pasta|spaghetti|linguine|fettuccine|penne|lasagna|carbonara|bolognese|gnocchi|mac and cheese|macaroni|alfredo|ravioli"),
    ("noodles", "ramen|pho|noodle|lo mein|pad thai|udon|soba|chow mein"),
    ("soup", "soup|bisque|chowder|broth|stew|gumbo|minestrone"),
    ("curry", "curry|masala|tikka|korma|biryani| dal |jambalaya|vindaloo"),
    ("rice", "risotto|paella|pilaf|fried rice|rice bowl|congee"),
    ("burger", "burger|slider|cheeseburger"),
    ("wrap", "shawarma|gyro|kebab|souvlaki|pita|wrap|spring roll"),
    ("sandwich", "sandwich|panini|sub |club|blt|hoagie|baguette|bagel"),
    ("salad", "salad|caprese|slaw|greens|caesar|cobb|tabbouleh|bowl"),
    ("dumpling", "dumpling|gyoza|potsticker|empanada|samosa|wonton|pierogi"),
    ("falafel", "falafel|kofta|meatball"),
    ("shrimp", "shrimp|prawn|tempura"),
    ("lobster", "lobster|crab|crawfish"),
    ("oyster", "oyster|clam|mussel|scallop"),
    ("fish", "salmon|tuna|cod|halibut|tilapia|trout|fish|seafood|ceviche|cedar|anchovy"),
    ("chicken", "chicken|poultry|wing|drumstick|nugget"),
    ("turkey", "turkey"),
    ("pork", "pork|bacon|sausage|chorizo|ham|prosciutto|pastrami|ribs|bratwurst"),
    ("beef", "steak|beef|brisket|barbacoa|ribeye|sirloin|filet|carne|bbq|barbecue|pulled|lamb|veal"),
    ("egg", "omelet|frittata|quiche|scramble|benedict|egg|brunch"),
    ("tofu", "tofu|tempeh|stir fry|stir-fry|teriyaki|edamame"),
    ("cheese", "charcuterie|cheese board|mozzarella|burrata|brie|fondue|caprese"),
    ("pancake", "pancake|waffle|french toast|crepe"),
    ("croissant", "croissant|pastry|danish|scone"),
    ("cake", "cake|cupcake|cheesecake|tiramisu"),
    ("pie", "pie|tart|cobbler"),
    ("cookie", "cookie|brownie|biscotti|macaron"),
    ("chocolate", "chocolate|fudge|truffle|ganache"),
    ("icecream", "ice cream|gelato|sorbet|sundae"),
    ("donut", "donut|doughnut"),
    ("custard", "custard|flan|pudding|panna cotta|dessert|honey|baklava"),
    ("fruit", "berry|strawberry|fruit|parfait|melon"),
    ("coffee", "coffee|espresso|latte|cappuccino|mocha"),
    ("tea", "matcha|green tea|chai| tea"),
    ("beer", "beer|ale|lager|ipa"),
    ("wine", "wine|sangria|rose|prosecco|champagne"),
    ("cocktail", "cocktail|margarita|mojito|punch|martini"),
    ("juice", "juice|smoothie|lemonade|soda|milkshake|shake"),
    ("avocado", "avocado|guacamole"),
    ("mushroom", "mushroom|portobello|shiitake"),
    ("corn", "corn|elote|cornbread"),
    ("potato", "potato|fries|mashed|hash"),
    ("tomato", "tomato|bruschetta|marinara"),
    ("chili", "chili|jalape|spicy|buffalo|sriracha"),
    ("veg", "broccoli|cauliflower|asparagus|brussels|kale|spinach|vegetable|veggie|carrot|zucchini|roasted veg"),
    ("eggplant", "eggplant|aubergine|ratatouille|parmigiana"),
    ("beans", "bean|chickpea|lentil|hummus|legume"),
    ("nuts", "peanut|almond|cashew|pistachio|walnut|pecan| nut"),
    ("hotdog", "hot dog|corn dog"),
]

# Leading boundary only — matching App.jsx's `new RegExp('\\b(' + k + ')', 'i')` exactly.
_FOOD_RE: list[tuple[str, re.Pattern[str]]] = [
    (cat, re.compile(r"\b(" + keys + ")", re.I)) for cat, keys in FOOD_RULES
]


def food_cat(text: str, diets: Iterable[str] = ()) -> str:
    """Photo category for a blob of dish text. Mirrors App.jsx `foodCat()` including its
    fallbacks: a vegan/vegetarian badge shows greens, everything else shows a platter."""
    blob = (text or "").lower()
    for cat, rx in _FOOD_RE:
        if rx.search(blob):
            return cat
    lowered = {str(d).lower() for d in diets}
    if "vegan" in lowered or "vegetarian" in lowered:
        return "salad"
    return "platter"


# Corrected leaf -> existing JPEG basename. The taxonomy grew past the photo set, so this
# is the lossy projection back onto the 53 bundled images. Mapping types onto filenames is
# safe; renaming the files to match the types would break every card in App.jsx.
PHOTO_KEY: dict[str, str] = {
    "pizza": "pizza", "taco": "taco", "burrito": "burrito", "wrap": "wrap",
    "sandwich": "sandwich", "burger": "burger", "flatbread": "wrap",
    "pasta": "pasta", "noodles": "noodles", "rice": "rice", "sushi": "sushi",
    "soup": "soup", "curry": "curry", "chili": "chili",
    "salad": "salad", "grain_bowl": "salad",
    "dumpling": "dumpling", "skewer": "chicken", "fritter": "falafel",
    "dip": "beans", "fried_bite": "shrimp", "platter": "platter",
    "poultry": "chicken", "beef": "beef", "pork": "pork", "seafood": "shrimp",
    "tofu": "tofu", "egg_dish": "egg", "vegetable": "veg", "potato": "potato",
    "cake": "cake", "cookie": "cookie", "pastry": "croissant", "pie": "pie",
    "frozen_dessert": "icecream", "chocolate": "chocolate", "custard": "custard",
    "fruit": "fruit",
    "coffee": "coffee", "tea": "tea", "juice": "juice", "alcohol": "wine",
    "other": "platter",
}


def photo_key(leaf: str) -> str:
    """JPEG basename under web/public/food/ for a dish_type leaf."""
    return PHOTO_KEY.get(leaf, "platter")


__all__ = [
    "DISH_TYPE_TREE", "DISH_TYPE_LEAVES", "DISH_TYPE_GROUPS", "PARENT_OF", "DISH_TYPE_RULES",
    "dish_type", "dish_type_group", "dish_type_for", "dish_type_group_for",
    "classify_all", "coverage",
    "MEAL_TYPES", "MEAL_TIME_OCCASIONS", "BREAKFAST_RE", "SNACKABLE",
    "meal_type", "meal_type_for", "occasions",
    "FOOD_RULES", "food_cat", "PHOTO_KEY", "photo_key",
]
