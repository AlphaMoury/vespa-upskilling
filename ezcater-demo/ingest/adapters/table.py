"""
CSV / Excel table -> MenuItem (source #4).

Any tabular menu/dish export (a spreadsheet, a POS extract, a database dump) flows through
the SAME enrich() -> ontology graph + e5 vector -> Vespa pipeline as a PDF or a recipe.
Column mapping is fuzzy/best-effort, so it handles arbitrary headers.

    python -m ingest.run_ingest --source table --path menus/sample_catering.csv --print
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterator, Optional

from ..menu_item import MenuItem

# our field -> candidate column names (lowercased, matched exactly after strip())
_COLMAP = {
    "name": ["name", "dish", "item", "title", "product", "menu item"],
    "description": ["description", "desc", "details", "notes"],
    "price": ["price", "cost", "amount", "total", "price ($)"],
    "serves": ["serves", "servings", "serving size", "yield", "people", "headcount", "feeds"],
    "cuisine": ["cuisine", "category", "type", "section", "style"],
    "caterer_name": ["caterer", "restaurant", "vendor", "supplier", "brand"],
    "ingredients": ["ingredients", "components"],
    "dietary": ["dietary", "diet", "diet labels", "diet_labels", "tags", "labels"],
    "course": ["course", "meal", "meal type", "meal_type"],
}


def _pick(row: dict, keys: list[str]) -> Optional[str]:
    for want in keys:
        for col, val in row.items():
            if col and str(col).strip().lower() == want and val not in (None, ""):
                return str(val).strip()
    return None


def _split(s: Optional[str]) -> list[str]:
    if not s:
        return []
    for sep in (";", "|", ","):
        if sep in s:
            return [x.strip() for x in s.split(sep) if x.strip()]
    return [s.strip()] if s.strip() else []


def _num(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return float(str(s).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


class TableAdapter:
    name = "table"

    def __init__(self, path: str | None = None):
        self.path = Path(path) if path else None

    def fetch(self, **opts) -> Iterator[dict]:
        p = self.path
        if not p or not p.is_file():
            return
        if p.suffix.lower() in (".xlsx", ".xls"):
            import openpyxl  # optional dependency
            ws = openpyxl.load_workbook(p, read_only=True, data_only=True).active
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                return
            header = [str(c).strip() if c is not None else "" for c in rows[0]]
            for r in rows[1:]:
                yield {header[i]: r[i] for i in range(min(len(header), len(r))) if header[i]}
        else:
            with open(p, newline="", encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    yield row

    def to_menu_items(self, rec: dict) -> list[MenuItem]:
        name = _pick(rec, _COLMAP["name"])
        if not name:
            return []
        price = _num(_pick(rec, _COLMAP["price"]))
        serves_f = _num(_pick(rec, _COLMAP["serves"]))
        serves = int(serves_f) if serves_f else None
        pp = round(price / serves, 2) if (price and serves) else (price if price and price < 60 else None)
        return [MenuItem(
            id="", name=name, description=_pick(rec, _COLMAP["description"]) or "",
            cuisine=_pick(rec, _COLMAP["cuisine"]), course=_pick(rec, _COLMAP["course"]),
            serves=serves, price=price, price_pp=pp,
            caterer_name=_pick(rec, _COLMAP["caterer_name"]), source="table",
            ingredients_raw=_split(_pick(rec, _COLMAP["ingredients"])),
            dietary_declared=_split(_pick(rec, _COLMAP["dietary"])),
        )]
