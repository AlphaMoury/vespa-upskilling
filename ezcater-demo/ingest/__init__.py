"""
ezcater-demo ingestion pipeline.

    source  --(adapter)-->  MenuItem  --(enrich)-->  Vespa `dish` doc

Public surface:
    from ingest import MenuItem, iter_items, enrich, REGISTRY
"""

from .menu_item import MenuItem, SourceAdapter, iter_items
from .enrich import enrich
from .adapters import REGISTRY
from . import config

__all__ = ["MenuItem", "SourceAdapter", "iter_items", "enrich", "REGISTRY", "config"]
