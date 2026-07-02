"""
The ONE common schema every source normalizes to, plus the pluggable adapter
interface. This is the load-bearing idea of the whole pipeline:

    heterogeneous source  --(adapter)-->  MenuItem  --(enrich)-->  Vespa `dish` doc

Source-specific messiness lives ONLY in adapters. Everything downstream (enrichment,
embedding, feeding, serving) is uniform because it only ever sees a MenuItem.

Mirrors ezCater's production shape: adapters are Kafka producers, the shared
enrichment stage is one Temporal workflow, Vespa is the sink.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Iterable, Iterator, Optional, Protocol, runtime_checkable
import hashlib


@dataclass
class MenuItem:
    """A single catering menu item, source-agnostic.

    `*_raw` / `*_declared` are what a source gave us verbatim. The enrichment stage
    (ingest/enrich.py) reads those and fills the denormalized fields Vespa filters on
    (ingredients, allergens, dietary, spice_level, flavor, occasion).
    """
    id: str
    name: str
    description: str = ""
    cuisine: Optional[str] = None
    course: Optional[str] = None            # appetizer | main | dessert | side
    serves: Optional[int] = None
    price: Optional[float] = None
    price_pp: Optional[float] = None
    caterer_name: Optional[str] = None
    popularity: int = 0
    source: str = ""                        # provenance: which adapter produced this

    # --- raw, pre-enrichment (what the source stated) ---
    ingredients_raw: list[str] = field(default_factory=list)
    dietary_declared: list[str] = field(default_factory=list)

    # --- enrichment fills these (the denormalized KG/ontology output) ---
    ingredients: list[str] = field(default_factory=list)   # normalized to vocabulary
    allergens: list[str] = field(default_factory=list)     # INFERRED -> hard filter
    dietary: list[str] = field(default_factory=list)       # vegan|vegetarian|gluten-free|...
    spice_level: int = 0
    flavor: str = "savory"
    occasion: list[str] = field(default_factory=list)

    # provenance / quality — sub-1.0 means "route to human review" (PDF/LLM sources)
    confidence: float = 1.0

    def stable_id(self) -> str:
        """A deterministic id if the source didn't give a good one."""
        if self.id:
            return self.id
        h = hashlib.sha1(f"{self.source}|{self.name}|{self.caterer_name}".encode()).hexdigest()[:12]
        return f"{self.source or 'item'}-{h}"

    def ingredient_key(self) -> str:
        """Hash of the ingredient set — the enrichment cache key. Allergen/diet labels
        are stable per ingredient set, so this lets re-indexing skip the LLM/API."""
        base = sorted(x.lower().strip() for x in (self.ingredients_raw or []) if x.strip())
        if not base:  # fall back to text when a source has no ingredient list
            base = [self.name.lower().strip(), (self.description or "").lower().strip()]
        return hashlib.sha1("|".join(base).encode()).hexdigest()

    def to_vespa_doc(self) -> dict:
        """Project onto the Vespa `dish` schema (only fields that schema declares)."""
        f = {
            "id": self.stable_id(),
            "name": self.name,
            "description": self.description or "",
            "cuisine": self.cuisine or "",
            "course": self.course or "",
            "dietary": self.dietary,
            "serves": int(self.serves) if self.serves else 0,
            "price": float(self.price) if self.price else 0.0,
            "price_pp": float(self.price_pp) if self.price_pp else 0.0,
            "caterer_name": self.caterer_name or "",
            "popularity": int(self.popularity or 0),
            "spice_level": int(self.spice_level or 0),
            "flavor": self.flavor or "savory",
            "occasion": self.occasion,
            "ingredients": self.ingredients,
            "allergens": self.allergens,
            "source": self.source or "",
        }
        return {"id": self.stable_id(), "fields": f}

    def as_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class SourceAdapter(Protocol):
    """Every ingestion source implements this. Two methods, one contract.

    fetch() pulls raw records from wherever the source lives (parquet rows, PDF pages,
    API responses, DB rows). to_menu_items() normalizes one raw record into >=0
    MenuItems. The pipeline (run_ingest.py) is written against this Protocol alone,
    so adding a source never touches enrichment or serving code.
    """
    name: str

    def fetch(self, **opts) -> Iterator[object]:
        ...

    def to_menu_items(self, rec: object) -> list[MenuItem]:
        ...


def iter_items(adapter: SourceAdapter, limit: Optional[int] = None, **opts) -> Iterator[MenuItem]:
    """Convenience: run an adapter end-to-end (fetch -> normalize), capped at `limit`."""
    n = 0
    for rec in adapter.fetch(**opts):
        for item in adapter.to_menu_items(rec):
            if not item.source:
                item.source = adapter.name
            yield item
            n += 1
            if limit and n >= limit:
                return
