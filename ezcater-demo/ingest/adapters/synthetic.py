"""
Synthetic catering catalog -> MenuItem (source #0, the baseline).

Reads the existing data/dishes.jsonl (built by data/build_dataset.py) and replays it
through the SAME MenuItem -> enrich -> Vespa path as every other source. This keeps the
original curated catering catalog in the demo while proving the pipeline is uniform.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from ..menu_item import MenuItem

_DISHES = Path(__file__).resolve().parents[2] / "data" / "dishes.jsonl"


class SyntheticCateringAdapter:
    name = "synthetic"

    def __init__(self, path: str | None = None):
        self.path = Path(path) if path else _DISHES

    def fetch(self, **opts) -> Iterator[dict]:
        if not self.path.is_file():
            return
        for line in self.path.read_text().splitlines():
            if line.strip():
                yield json.loads(line)

    def to_menu_items(self, rec: dict) -> list[MenuItem]:
        f = rec.get("fields", rec)
        return [MenuItem(
            id=str(f.get("id") or rec.get("id") or ""),
            name=f.get("name") or "", description=f.get("description") or "",
            cuisine=f.get("cuisine"), course=f.get("course"),
            serves=f.get("serves"), price=f.get("price"), price_pp=f.get("price_pp"),
            caterer_name=f.get("caterer_name"), popularity=f.get("popularity") or 0,
            source="synthetic", dietary_declared=f.get("dietary") or [],
            ingredients_raw=f.get("ingredients") or [],
        )]
