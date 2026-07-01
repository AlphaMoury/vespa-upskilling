"""
Generate an EzCater-style catering dataset: caterers (suppliers) + their dishes (menu items).

Real dish names across cuisines (the "real recipes" part) + synthetic caterers (the
marketplace part). Writes two JSONL files the deploy script feeds into two Vespa schemas:

    data/caterers.jsonl   {"id":..,"fields":{...}}
    data/dishes.jsonl     {"id":..,"fields":{...}}

We deliberately write rich descriptions with synonyms so that AI hybrid search can beat
plain keyword search (e.g. a query "healthy plant-based lunch" finds vegan bowls/salads
whose text never says "healthy").

    python build_dataset.py            # default ~100 caterers
    CATERERS=150 python build_dataset.py
"""

import json
import os
import random
from pathlib import Path

random.seed(42)  # deterministic

HERE = Path(__file__).resolve().parent
CATERERS = int(os.environ.get("CATERERS", "100"))

# cuisine -> (dishes[(name, course, [dietary], description)], typical price range per head)
CUISINES = {
    "Italian": dict(price=(12, 28), dishes=[
        ("Margherita Pizza", "main", ["vegetarian"], "Wood-fired pizza with San Marzano tomato, fresh mozzarella and basil."),
        ("Chicken Parmigiana", "main", [], "Breaded chicken cutlets baked with marinara and melted mozzarella."),
        ("Penne alla Vodka", "main", ["vegetarian"], "Penne in a creamy tomato vodka sauce, a crowd-pleasing comfort pasta."),
        ("Eggplant Caponata", "appetizer", ["vegan", "gluten-free"], "Sweet-and-sour Sicilian eggplant relish with capers and olives."),
        ("Caprese Skewers", "appetizer", ["vegetarian", "gluten-free"], "Cherry tomato, mozzarella and basil bites drizzled with balsamic glaze."),
        ("Tiramisu Cups", "dessert", ["vegetarian"], "Espresso-soaked ladyfingers layered with mascarpone cream."),
    ]),
    "Mexican": dict(price=(10, 22), dishes=[
        ("Chicken Tinga Tacos", "main", ["gluten-free"], "Smoky shredded chicken in chipotle tomato sauce on warm corn tortillas."),
        ("Veggie Burrito Bowl", "main", ["vegan", "gluten-free"], "Cilantro-lime rice, black beans, fajita veggies, salsa and guacamole."),
        ("Carnitas Tacos", "main", ["gluten-free"], "Slow-braised pork shoulder with onion, cilantro and lime."),
        ("Loaded Nachos", "appetizer", ["vegetarian"], "Tortilla chips piled with cheese, beans, jalapeños and pico de gallo."),
        ("Guacamole & Chips", "appetizer", ["vegan", "gluten-free"], "Hand-mashed avocado with lime and cilantro, plant-based and fresh."),
        ("Churros", "dessert", ["vegetarian"], "Cinnamon-sugar fried dough sticks with chocolate dipping sauce."),
    ]),
    "Japanese": dict(price=(14, 32), dishes=[
        ("Assorted Sushi Platter", "main", ["dairy-free"], "Chef's selection of nigiri and maki rolls with fresh fish and rice."),
        ("Chicken Teriyaki Bowl", "main", ["dairy-free"], "Grilled chicken glazed in teriyaki over steamed rice and greens."),
        ("Vegetable Tempura", "appetizer", ["vegetarian"], "Lightly battered seasonal vegetables fried crisp, served with dipping sauce."),
        ("Edamame", "appetizer", ["vegan", "gluten-free"], "Steamed young soybeans tossed with sea salt, a light plant-based starter."),
        ("Miso Soup", "appetizer", ["vegan"], "Warming soybean-paste broth with tofu and scallions."),
        ("Mochi Ice Cream", "dessert", ["vegetarian", "gluten-free"], "Chewy rice-cake bites filled with ice cream."),
    ]),
    "Indian": dict(price=(11, 24), dishes=[
        ("Chicken Tikka Masala", "main", ["gluten-free"], "Char-grilled chicken simmered in a spiced creamy tomato gravy."),
        ("Chana Masala", "main", ["vegan", "gluten-free"], "Chickpeas stewed with onion, tomato and warm spices, hearty and plant-based."),
        ("Paneer Tikka", "appetizer", ["vegetarian", "gluten-free"], "Marinated cottage-cheese cubes grilled with peppers and onions."),
        ("Vegetable Samosas", "appetizer", ["vegan"], "Crisp pastry pockets filled with spiced potato and peas."),
        ("Garlic Naan", "main", ["vegetarian"], "Soft tandoor-baked flatbread brushed with garlic butter."),
        ("Gulab Jamun", "dessert", ["vegetarian"], "Warm milk-dough dumplings soaked in rose-cardamom syrup."),
    ]),
    "Thai": dict(price=(12, 26), dishes=[
        ("Pad Thai", "main", ["gluten-free"], "Stir-fried rice noodles with egg, peanuts, tamarind and lime."),
        ("Green Curry with Tofu", "main", ["vegan", "gluten-free"], "Coconut green curry with tofu and vegetables, fragrant and plant-based."),
        ("Chicken Satay", "appetizer", ["gluten-free", "dairy-free"], "Grilled marinated chicken skewers with peanut dipping sauce."),
        ("Spring Rolls", "appetizer", ["vegan"], "Fresh rice-paper rolls packed with herbs and crunchy vegetables."),
        ("Tom Yum Soup", "appetizer", ["gluten-free"], "Hot-and-sour lemongrass broth with shrimp and mushrooms."),
        ("Mango Sticky Rice", "dessert", ["vegan", "gluten-free"], "Sweet coconut sticky rice with fresh mango slices."),
    ]),
    "Mediterranean": dict(price=(11, 25), dishes=[
        ("Mediterranean Mezze Platter", "appetizer", ["vegetarian"], "Hummus, baba ganoush, olives, feta and warm pita for sharing."),
        ("Falafel Wrap", "main", ["vegan"], "Crispy chickpea fritters with tahini, salad and pickles in flatbread."),
        ("Chicken Shawarma Bowl", "main", ["gluten-free", "dairy-free"], "Spit-roasted spiced chicken over rice with garlic sauce and salad."),
        ("Greek Salad", "appetizer", ["vegetarian", "gluten-free"], "Tomato, cucumber, olives and feta with oregano and olive oil."),
        ("Quinoa Tabbouleh", "appetizer", ["vegan", "gluten-free"], "Bright herb-and-quinoa salad with lemon, a wholesome plant-based side."),
        ("Baklava", "dessert", ["vegetarian"], "Layered phyllo with walnuts and honey syrup."),
    ]),
    "American": dict(price=(10, 24), dishes=[
        ("Classic Cheeseburger Sliders", "main", [], "Mini beef burgers with cheddar, pickles and special sauce on brioche."),
        ("BBQ Pulled Pork", "main", ["gluten-free", "dairy-free"], "Slow-smoked pork in tangy barbecue sauce, served by the tray."),
        ("Buffalo Cauliflower Bites", "appetizer", ["vegan"], "Crispy roasted cauliflower tossed in spicy buffalo sauce, plant-based."),
        ("Caesar Salad", "appetizer", ["vegetarian"], "Romaine, parmesan and croutons in a creamy Caesar dressing."),
        ("Mac & Cheese", "main", ["vegetarian"], "Three-cheese baked macaroni, ultimate comfort food."),
        ("Chocolate Chip Cookies", "dessert", ["vegetarian"], "Soft-baked cookies loaded with chocolate chunks."),
    ]),
    "Chinese": dict(price=(11, 23), dishes=[
        ("Kung Pao Chicken", "main", ["dairy-free"], "Wok-fried chicken with peanuts, chili and scallions."),
        ("Vegetable Lo Mein", "main", ["vegan"], "Stir-fried noodles with crisp vegetables in a savory sauce."),
        ("Pork Dumplings", "appetizer", ["dairy-free"], "Pan-seared dumplings with a soy-vinegar dipping sauce."),
        ("Mapo Tofu", "main", ["vegetarian"], "Silken tofu in a spicy fermented-bean sauce."),
        ("Vegetable Fried Rice", "main", ["vegetarian"], "Egg-fried rice tossed with peas, carrots and scallions."),
        ("Fortune Cookies", "dessert", ["vegan"], "Crisp folded cookies with a message inside."),
    ]),
    "Salads & Bowls": dict(price=(10, 18), dishes=[
        ("Vegan Buddha Bowl", "main", ["vegan", "gluten-free"], "Roasted vegetables, quinoa, chickpeas and tahini, a wholesome nutritious bowl."),
        ("Kale & Quinoa Power Salad", "main", ["vegan", "gluten-free"], "Superfood greens with quinoa, almonds and a lemon vinaigrette, light and good-for-you."),
        ("Grilled Chicken Cobb", "main", ["gluten-free"], "Greens with grilled chicken, egg, avocado and blue cheese."),
        ("Mediterranean Grain Bowl", "main", ["vegetarian"], "Farro, roasted veg, feta and herby dressing, a balanced midday meal."),
        ("Harvest Apple Salad", "appetizer", ["vegetarian", "gluten-free"], "Mixed greens, apple, candied pecans and goat cheese."),
        ("Acai Fruit Cups", "dessert", ["vegan", "gluten-free"], "Antioxidant acai blended and topped with fresh fruit and granola."),
    ]),
    "Breakfast": dict(price=(8, 16), dishes=[
        ("Assorted Bagels & Spreads", "main", ["vegetarian"], "Fresh bagels with cream cheese, butter and jam for the morning."),
        ("Veggie Egg Frittata", "main", ["vegetarian", "gluten-free"], "Baked eggs with peppers, spinach and cheese, sliced for sharing."),
        ("Fresh Fruit Platter", "appetizer", ["vegan", "gluten-free"], "Seasonal melon, berries and grapes, a refreshing healthy start."),
        ("Yogurt & Granola Parfaits", "main", ["vegetarian"], "Creamy yogurt layered with granola and berries."),
        ("Avocado Toast Bar", "main", ["vegan"], "Toasted sourdough with smashed avocado and toppings, build-your-own."),
        ("Cinnamon Rolls", "dessert", ["vegetarian"], "Warm gooey rolls with cream-cheese icing."),
    ]),
}

ADJ = ["Premium", "Office", "Executive", "Deluxe", "Classic", "Party-Size", "Build-Your-Own", "Family-Style"]
CITIES = ["Boston, MA", "New York, NY", "Chicago, IL", "Austin, TX", "San Francisco, CA",
          "Seattle, WA", "Denver, CO", "Atlanta, GA", "Los Angeles, CA", "Washington, DC"]
CATERER_SUFFIX = ["Catering Co.", "Kitchen", "Caterers", "Catering", "Eats", "Provisions", "Table", "Foods"]
CATERER_PREFIX = ["Bella", "Golden", "Urban", "Hearth", "Garden", "Maple", "Coastal", "Sunrise",
                  "Harvest", "Spice", "Olive", "Bamboo", "Crimson", "Saffron", "Verde", "Copper",
                  "Lotus", "Twin Oaks", "Riverside", "Marigold"]


def caterer_name(cuisine, i):
    return f"{random.choice(CATERER_PREFIX)} {cuisine.split(' ')[0]} {random.choice(CATERER_SUFFIX)}"


# ---------------------------------------------------------------------------
# FOOD ONTOLOGY enrichment (the JD's "backend data enrichment" use case).
# In production this is LLM-generated (see build_ontology.py); here we derive a
# clean structured ontology from the dish text so query understanding has
# concrete fields to target. Fields: spice_level, flavor, occasion, ingredients,
# allergens. This is what turns flat menu text into a searchable food graph.
# ---------------------------------------------------------------------------
SPICE = {"mapo": 3, "kung pao": 2, "gochujang": 2, "buffalo": 2, "curry": 2,
         "tikka": 1, "masala": 1, "satay": 1, "kimchi": 1, "chipotle": 1, "tinga": 1, "shawarma": 1}
ALLERGEN_KW = {
    "gluten": ["bread", "pita", "naan", "pasta", "penne", "noodle", "tortilla", "bun", "brioche",
               "phyllo", "dough", "crouton", "wrap", "bagel", "roll", "cracker", "ladyfinger"],
    "dairy": ["cheese", "cream", "mozzarella", "feta", "yogurt", "butter", "mascarpone", "paneer",
              "milk", "parmesan", "ricotta", "goat cheese", "icing"],
    "nuts": ["peanut", "almond", "walnut", "pecan", "cashew", "pistachio"],
    "shellfish": ["shrimp", "prawn"],
    "soy": ["tofu", "soy", "edamame", "teriyaki", "miso"],
}
INGRED_KW = ["chicken", "pork", "beef", "tofu", "chickpea", "rice", "noodle", "hummus", "falafel",
             "avocado", "mozzarella", "tomato", "basil", "eggplant", "olive", "feta", "shrimp", "fish",
             "egg", "quinoa", "kale", "bean", "lentil", "paneer", "mango", "coconut", "peanut", "potato",
             "spinach", "pita", "cucumber", "salmon", "cauliflower", "granola", "berry", "apple", "pecan"]


def enrich(name, desc, cuisine, course, dietary):
    t = (name + " " + desc).lower()
    spice = 0
    for k, v in SPICE.items():
        if k in t:
            spice = max(spice, v)
    if "spicy" in t or "chili" in t:
        spice = max(spice, 2)

    allergens = set()
    for a, kws in ALLERGEN_KW.items():
        if any(k in t for k in kws):
            allergens.add(a)
    if "gluten-free" in dietary:
        allergens.discard("gluten")
    if "vegan" in dietary or "dairy-free" in dietary:
        allergens.discard("dairy")

    occ = set()
    if course == "breakfast" or cuisine == "Breakfast":
        occ |= {"breakfast", "morning"}
    if course == "dessert":
        occ |= {"celebration", "treat"}
    if cuisine == "Salads & Bowls" or any(k in t for k in ["quinoa", "kale", "buddha", "salad", "healthy", "wholesome", "nutritious", "light", "good-for-you"]):
        occ |= {"healthy", "light"}
    if cuisine in ("Japanese", "Indian", "Italian", "Mediterranean") and course in ("main", "appetizer"):
        occ |= {"client", "impressive", "dinner"}
    if any(k in t for k in ["mac", "bbq", "pulled pork", "fried", "cheeseburger", "pizza", "comfort"]):
        occ |= {"comfort", "team"}
    if not occ:
        occ |= {"lunch", "team"}

    flavor = "sweet" if course == "dessert" else ("spicy" if spice >= 2 else ("fresh" if cuisine == "Salads & Bowls" else "savory"))
    ingredients = sorted({w for w in INGRED_KW if w in t})
    return {
        "spice_level": spice,
        "flavor": flavor,
        "occasion": sorted(occ),
        "ingredients": ingredients,
        "allergens": sorted(allergens),
    }


def main():
    caterers, dishes = [], []
    cuisine_list = list(CUISINES)
    did = 0
    for ci in range(CATERERS):
        cuisine = cuisine_list[ci % len(cuisine_list)]
        cid = f"c{ci}"
        city = random.choice(CITIES)
        rating = round(random.uniform(3.8, 5.0), 1)
        min_order = random.choice([100, 150, 200, 250, 300])
        lead = random.choice([24, 36, 48, 72])
        cname = caterer_name(cuisine, ci)
        blurb = (f"{cuisine} catering for offices and events in {city.split(',')[0]}. "
                 f"Fresh, made-to-order trays and platters for team lunches, meetings and celebrations.")
        caterers.append({"id": cid, "fields": {
            "id": cid, "name": cname, "cuisine": cuisine, "city": city,
            "rating": rating, "min_order": min_order, "lead_time": lead, "blurb": blurb,
        }})
        # each caterer offers its cuisine's dishes (+ a couple of crossovers)
        menu = list(CUISINES[cuisine]["dishes"])
        lo, hi = CUISINES[cuisine]["price"]
        for (name, course, dietary, desc) in menu:
            did += 1
            label = f"{random.choice(ADJ)} {name}" if random.random() < 0.35 else name
            serves = random.choice([5, 10, 12, 20, 25, 50])
            price = round(random.uniform(lo, hi) * serves * random.uniform(0.9, 1.1), 2)  # total; price_pp ~= per head
            onto = enrich(name, desc, cuisine, course, dietary)
            dishes.append({"id": f"d{did}", "fields": {
                "id": f"d{did}", "name": label, "description": desc, "cuisine": cuisine,
                "course": course, "dietary": dietary, "serves": serves, "price": price,
                "price_pp": round(price / serves, 2),
                "caterer_id": cid, "caterer_name": cname,
                "popularity": random.randint(0, 100),
                **onto,
            }})

    out = HERE
    (out / "caterers.jsonl").write_text("\n".join(json.dumps(c) for c in caterers) + "\n")
    (out / "dishes.jsonl").write_text("\n".join(json.dumps(d) for d in dishes) + "\n")
    print(f"wrote {len(caterers)} caterers -> caterers.jsonl")
    print(f"wrote {len(dishes)} dishes    -> dishes.jsonl")
    cuisines = sorted({c['fields']['cuisine'] for c in caterers})
    print(f"cuisines: {', '.join(cuisines)}")


if __name__ == "__main__":
    main()
