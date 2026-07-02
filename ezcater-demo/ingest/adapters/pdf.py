"""
Menu PDF -> MenuItem (source #2, the showpiece).

This is the exact problem ezCater solves by hand today: a caterer emails a branded PDF
and a human transcribes it. We automate that:

  * index_llm_enabled()  -> render pages to images, hand them to a vision-LLM with a
                            strict JSON schema, NEVER invent prices/allergens, null on
                            missing, per-item confidence (< 0.7 => flag for review).
  * no key               -> deterministic text extraction (fitz get_text) + a heuristic
                            line parser. Degraded but real — the pipeline still runs.

Drop any real caterer PDF into ezcater-demo/menus/ and it gets ingested.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Iterator

from ..menu_item import MenuItem
from ..config import index_llm_enabled
from .. import llm

_MAX_PAGES = 5          # cap pages per PDF (bounds vision tokens)
_DPI = 140

_VISION_SYS = (
    "You transcribe a catering/restaurant MENU image into structured data. "
    "Return ONLY JSON: {\"caterer\": string|null, \"items\": [{"
    "\"name\": string, \"description\": string|null, \"price\": number|null, "
    "\"serves\": integer|null, \"section\": string|null, "
    "\"dietary_tags\": [string], \"ingredients\": [string], \"confidence\": number}]}. "
    "RULES: transcribe only what is printed. NEVER invent prices, allergens, or ingredients. "
    "Use null for anything not shown. confidence is 0-1 for how sure you are of that item. "
    "Ignore decorative text, phone numbers, and addresses."
)


def _course_from_section(section: str, name: str) -> str:
    t = f"{section} {name}".lower()
    if any(k in t for k in ["dessert", "sweet", "cookie", "cake"]):
        return "dessert"
    if any(k in t for k in ["breakfast", "brunch", "morning"]):
        return "breakfast"
    if any(k in t for k in ["appetizer", "starter", "salad", "soup", "side", "snack", "dip", "small"]):
        return "appetizer"
    return "main"


class MenuPDFAdapter:
    """SourceAdapter over PDF files in a directory (or an explicit list)."""
    name = "pdf"

    def __init__(self, path: str | None = None, max_pages: int = _MAX_PAGES):
        self.dir = Path(path) if path else (Path(__file__).resolve().parents[2] / "menus")
        self.max_pages = max_pages

    def _pdf_paths(self) -> list[Path]:
        if self.dir.is_file() and self.dir.suffix.lower() == ".pdf":
            return [self.dir]
        return sorted(self.dir.glob("*.pdf")) if self.dir.is_dir() else []

    def fetch(self, **opts) -> Iterator[dict]:
        import fitz  # PyMuPDF
        for pdf in self._pdf_paths():
            try:
                doc = fitz.open(pdf)
            except Exception:  # noqa: BLE001
                continue
            images_b64, pages_text = [], []
            for page in list(doc)[: self.max_pages]:
                try:
                    pix = page.get_pixmap(dpi=_DPI)
                    images_b64.append(base64.b64encode(pix.tobytes("png")).decode())
                    pages_text.append(page.get_text())
                except Exception:  # noqa: BLE001
                    pass
            doc.close()
            yield {"path": str(pdf), "caterer_from_name": pdf.stem.replace("_", " ").title(),
                   "images_b64": images_b64, "pages_text": pages_text}

    def to_menu_items(self, rec: dict) -> list[MenuItem]:
        if index_llm_enabled() and rec.get("images_b64"):
            items = self._via_vision(rec)
            if items:
                return items
        return self._via_text(rec)  # deterministic fallback

    # ---- LLM path ----
    def _via_vision(self, rec: dict) -> list[MenuItem]:
        stem = Path(rec["path"]).stem
        user = "Transcribe every menu item across these page image(s) into the JSON schema."
        data = llm.vision_json(_VISION_SYS, user, rec["images_b64"][: self.max_pages]) or {}
        caterer = data.get("caterer") or rec["caterer_from_name"]
        out = []
        for i, it in enumerate(data.get("items", []) or []):
            name = (it.get("name") or "").strip()
            if not name:
                continue
            serves = it.get("serves")
            price = it.get("price")
            pp = round(float(price) / int(serves), 2) if price and serves else (float(price) if price else None)
            section = it.get("section") or ""
            out.append(MenuItem(
                id=f"pdf-{stem}-{i}", name=name, description=(it.get("description") or "").strip(),
                cuisine=None, course=_course_from_section(section, name),
                serves=int(serves) if serves else None, price=float(price) if price else None, price_pp=pp,
                caterer_name=caterer, source="pdf:vision",
                ingredients_raw=[str(x) for x in (it.get("ingredients") or [])],
                dietary_declared=[str(x) for x in (it.get("dietary_tags") or [])],
                confidence=float(it.get("confidence") or 0.8),
            ))
        return out

    # ---- deterministic fallback (no key) ----
    _PRICE = re.compile(r"\$\s?(\d{1,4}(?:\.\d{2})?)")
    _SERVES = re.compile(r"serves\s+(\d+)", re.I)

    def _price_only(self, line: str) -> float | None:
        """A line that is essentially just a price (menus put prices on their own line)."""
        m = self._PRICE.search(line)
        if m and len(self._PRICE.sub("", line).strip(" .-–—\t")) <= 2:
            return float(m.group(1))
        return None

    def _is_section(self, line: str) -> bool:
        letters = [c for c in line if c.isalpha()]
        return bool(letters) and line.upper() == line and self._PRICE.search(line) is None and len(line) < 40

    def _emit(self, out, stem, section, name, price, desc):
        sm = self._SERVES.search(desc or "")
        serves = int(sm.group(1)) if sm else None
        pp = round(price / serves, 2) if (price and serves) else (price if price and price < 40 else None)
        out.append(MenuItem(
            id=f"pdf-{stem}-{len(out)+1}", name=name[:120], description=(desc or "")[:300],
            cuisine=None, course=_course_from_section(section, name),
            serves=serves, price=price, price_pp=pp,
            caterer_name=None, source="pdf:text", confidence=0.5,
        ))

    def _via_text(self, rec: dict) -> list[MenuItem]:
        stem = Path(rec["path"]).stem
        out, section = [], ""
        for text in rec.get("pages_text", []):
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            i = 0
            while i < len(lines):
                line = lines[i]
                if self._is_section(line):
                    section = line.title(); i += 1; continue
                # common layout: NAME line, then PRICE-only line, then DESCRIPTION line
                nxt_price = self._price_only(lines[i + 1]) if i + 1 < len(lines) else None
                if nxt_price is not None and len(line) >= 3 and self._PRICE.search(line) is None:
                    desc = ""
                    j = i + 2
                    if j < len(lines) and self._price_only(lines[j]) is None and not self._is_section(lines[j]):
                        desc = lines[j]; j += 1
                    self._emit(out, stem, section, line, nxt_price, desc)
                    i = j; continue
                # or price on the same line as the name
                m = self._PRICE.search(line)
                if m:
                    name = self._PRICE.sub("", line).strip(" .-–—\t")
                    if len(name) >= 3:
                        self._emit(out, stem, section, name, float(m.group(1)), "")
                i += 1
        # attach a caterer name from the doc header if we saw one
        caterer = rec.get("caterer_from_name")
        for it in out:
            it.caterer_name = caterer
        return out
