"""
The food ontology as a REAL graph (NetworkX), not just a dict.

Nodes are typed: ingredient | allergen | diet | cuisine.
Edges are typed:
    ingredient --CONTAINS--> allergen      (cashew -> nuts)
    ingredient --CONFLICTS--> diet          (chicken -> vegan, vegetarian; cheese -> vegan)
    cuisine    --FEATURES--> ingredient      (mediterranean -> hummus, tahini, ...)

It is SEEDED from the curated taxonomy (so it's never empty and stays auditable) and
GROWN by the LLM at ingest: when a menu names an ingredient the graph doesn't know, we
ask the LLM to classify it once (cached) and append nodes/edges. This is how the ontology
scales as new restaurants/menus arrive — no new hand-written rules.

It is used in two places:
  * INDEX time  — enrich.py aggregates allergens/diet-conflicts for an item's ingredients.
  * QUERY time  — expand a query's concepts (cuisine -> featured terms, allergen -> its
                  ingredient set) before the Vespa hybrid runs.

Persisted to data/ontology_graph.json so growth survives across runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import networkx as nx

from . import taxonomy, cache, llm
from .config import index_llm_enabled

_PATH = Path(__file__).resolve().parent.parent / "data" / "ontology_graph.json"

# cuisine -> representative ingredients/dishes (seed for query-time expansion)
CUISINE_FEATURES = {
    "italian": ["pasta", "mozzarella", "basil", "parmesan", "tomato", "pesto", "risotto"],
    "mexican": ["tortilla", "beans", "avocado", "salsa", "guacamole", "cilantro", "carnitas"],
    "japanese": ["sushi", "rice", "teriyaki", "edamame", "miso", "tempura"],
    "indian": ["curry", "paneer", "chickpea", "tikka", "masala", "naan", "lentil"],
    "thai": ["curry", "coconut", "peanut", "noodle", "lemongrass", "satay", "tofu"],
    "mediterranean": ["hummus", "falafel", "tahini", "feta", "olive", "pita", "tabbouleh"],
    "american": ["burger", "bbq", "cheese", "fried", "mac", "pulled pork"],
    "chinese": ["noodle", "tofu", "dumpling", "fried rice", "soy sauce", "kung pao"],
    "breakfast": ["bagel", "egg", "yogurt", "granola", "avocado", "fruit", "frittata"],
}

# a positive-inclusion category (from concepts.include) -> member ingredients/dishes, so
# "with meat" broadens the vector query toward actual meat dishes (shawarma, kebab, brisket…)
_CATEGORY_EXPAND = {
    "meat": ["chicken", "beef", "pork", "lamb", "brisket", "shawarma", "kebab", "carnitas", "sausage", "meatball", "steak"],
    "poultry": ["chicken", "turkey"],
    "seafood": ["salmon", "shrimp", "tuna", "crab", "fish"],
    "fish": ["salmon", "tuna", "cod"],
    "cheese": ["mozzarella", "feta", "parmesan", "cheddar"],
}

_LLM_SYS = (
    "Classify a single food INGREDIENT for a catering ontology. Return ONLY JSON with keys: "
    "allergens (array from: gluten,dairy,eggs,nuts,peanuts,soy,shellfish,fish,sesame), "
    "diets_violated (array from: vegan,vegetarian — vegan is violated by any animal product; "
    "vegetarian is violated by meat/poultry/seafood only), "
    "category (short lowercase noun, e.g. 'vegetable','cheese','poultry'). "
    "Only assert what is certain for the base ingredient; prefer empty arrays over guessing."
)


class OntologyGraph:
    def __init__(self, g: Optional[nx.DiGraph] = None):
        self.g = g if g is not None else nx.DiGraph()

    # ---------- construction ----------
    def _node(self, name: str, kind: str) -> str:
        key = f"{kind}:{name.lower().strip()}"
        if key not in self.g:
            self.g.add_node(key, kind=kind, name=name.lower().strip())
        return key

    def _edge(self, a: str, b: str, rel: str):
        self.g.add_edge(a, b, rel=rel)

    def add_ingredient(self, name: str, allergens=(), diets_violated=(), category=None):
        ing = self._node(name, "ingredient")
        if category:
            self.g.nodes[ing]["category"] = category
        for a in allergens:
            self._edge(ing, self._node(a, "allergen"), "CONTAINS")
        for d in diets_violated:
            self._edge(ing, self._node(d, "diet"), "CONFLICTS")
        return ing

    def seed_from_taxonomy(self):
        """Build the base graph from the curated taxonomy — the auditable spine."""
        for ing, allergens in taxonomy.INGREDIENT_ALLERGENS.items():
            self.add_ingredient(ing, allergens=allergens)
        for ing in taxonomy.MEAT | taxonomy.SEAFOOD:
            self.add_ingredient(ing, diets_violated=("vegan", "vegetarian"))
        for ing in taxonomy.ANIMAL_NONMEAT:
            self.add_ingredient(ing, diets_violated=("vegan",))
        for cuisine, feats in CUISINE_FEATURES.items():
            cnode = self._node(cuisine, "cuisine")
            for f in feats:
                self._edge(cnode, self._node(f, "ingredient"), "FEATURES")
        return self

    # ---------- growth (LLM, cached) ----------
    def ensure_ingredient(self, name: str) -> str:
        """Return the ingredient node, classifying + appending it if unknown.
        Uses the LLM only when index-time LLM is enabled; else the taxonomy fallback."""
        key = f"ingredient:{name.lower().strip()}"
        if key in self.g:
            return key
        allergens, diets, cat = self._classify(name)
        return self.add_ingredient(name, allergens=allergens, diets_violated=diets, category=cat)

    def _classify(self, name: str):
        ckey = f"gcls:{name.lower().strip()}"
        hit = cache.get_json(ckey)
        if hit is not None:
            return set(hit.get("allergens", [])), set(hit.get("diets_violated", [])), hit.get("category")
        allergens, diets, cat = set(), set(), None
        if index_llm_enabled():
            data = llm.chat_json(_LLM_SYS, f"Ingredient: {name}") or {}
            allergens = {a for a in data.get("allergens", []) if a in taxonomy.ALLERGENS}
            diets = {d for d in data.get("diets_violated", []) if d in ("vegan", "vegetarian")}
            cat = data.get("category")
        if not allergens:                                  # deterministic fallback / backstop
            allergens = taxonomy.allergens_for([name])
        low = name.lower()
        if taxonomy._word_variants(low) & (taxonomy.MEAT | taxonomy.SEAFOOD):
            diets |= {"vegan", "vegetarian"}
        elif taxonomy._word_variants(low) & taxonomy.ANIMAL_NONMEAT:
            diets |= {"vegan"}
        cache.set_json(ckey, {"allergens": sorted(allergens), "diets_violated": sorted(diets), "category": cat})
        return allergens, diets, cat

    # ---------- index-time query ----------
    def allergens_of(self, ingredient: str) -> set[str]:
        node = f"ingredient:{ingredient.lower().strip()}"
        if node in self.g:
            return {self.g.nodes[t]["name"] for _, t in self.g.out_edges(node) if self.g[node][t]["rel"] == "CONTAINS"}
        # fallback: taxonomy handles plurals ('cashews') + multi-word ('soy sauce') + unknowns
        return taxonomy.allergens_for([ingredient])

    def diet_conflicts(self, ingredient: str) -> set[str]:
        node = f"ingredient:{ingredient.lower().strip()}"
        if node in self.g:
            return {self.g.nodes[t]["name"] for _, t in self.g.out_edges(node) if self.g[node][t]["rel"] == "CONFLICTS"}
        v = taxonomy._word_variants(ingredient.lower())   # plural-aware fallback
        if v & (taxonomy.MEAT | taxonomy.SEAFOOD):
            return {"vegan", "vegetarian"}
        if v & taxonomy.ANIMAL_NONMEAT:
            return {"vegan"}
        return set()

    def enrich(self, ingredients: list[str], grow: bool = False) -> dict:
        """Aggregate allergens + diet-conflicts for an item's ingredients."""
        allergens, conflicts = set(), set()
        for ing in ingredients:
            if grow:
                self.ensure_ingredient(ing)
            allergens |= self.allergens_of(ing)
            conflicts |= self.diet_conflicts(ing)
        return {"allergens": allergens, "diet_conflicts": conflicts}

    # ---------- query-time expansion ----------
    def expand_cuisine(self, cuisine: str) -> list[str]:
        node = f"cuisine:{(cuisine or '').lower().strip()}"
        if node not in self.g:
            return []
        return sorted(self.g.nodes[t]["name"] for _, t in self.g.out_edges(node) if self.g[node][t]["rel"] == "FEATURES")

    def ingredients_for_allergen(self, allergen: str) -> list[str]:
        node = f"allergen:{(allergen or '').lower().strip()}"
        if node not in self.g:
            return []
        return sorted(self.g.nodes[s]["name"] for s, _ in self.g.in_edges(node) if self.g[s][node]["rel"] == "CONTAINS")

    def expand_query(self, concepts: dict) -> dict:
        """Given understood concepts, return graph-expanded terms + allergen ingredient sets.
        cuisine -> featured terms (broaden recall); include (e.g. 'meat') -> its member ingredients
        (boosts wanted items in the vector leg); exclude_allergens -> their ingredients (explain)."""
        terms: list[str] = []
        if concepts.get("cuisine"):
            terms += self.expand_cuisine(concepts["cuisine"])
        include_terms: list[str] = []
        for it in (concepts.get("include") or []):
            include_terms += _CATEGORY_EXPAND.get(str(it).lower().strip(), [str(it)])
        terms += include_terms
        allergen_ings = {a: self.ingredients_for_allergen(a) for a in (concepts.get("exclude_allergens") or [])}
        return {"added_terms": sorted(set(terms)), "allergen_ingredients": allergen_ings,
                "include_terms": sorted(set(include_terms))}

    # ---------- persistence / stats ----------
    def stats(self) -> dict:
        kinds: dict[str, int] = {}
        for _, d in self.g.nodes(data=True):
            kinds[d.get("kind", "?")] = kinds.get(d.get("kind", "?"), 0) + 1
        return {**kinds, "edges": self.g.number_of_edges()}

    def save(self, path: Path = _PATH):
        data = nx.node_link_data(self.g, edges="edges")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    @classmethod
    def load(cls, path: Path = _PATH) -> "OntologyGraph":
        if path.is_file():
            try:
                g = nx.node_link_graph(json.loads(path.read_text()), directed=True, edges="edges")
                return cls(g)
            except Exception:  # noqa: BLE001
                pass
        return cls().seed_from_taxonomy()


# a process-wide singleton (seeded on first import)
_GRAPH: Optional[OntologyGraph] = None


def get_graph() -> OntologyGraph:
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = OntologyGraph.load()
    return _GRAPH
