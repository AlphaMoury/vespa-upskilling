"""Source adapters. Each turns one heterogeneous source into MenuItems.

Register new adapters here so run_ingest.py can look them up by name.
"""

from .hf import HFRecipeAdapter
from .pdf import MenuPDFAdapter
from .synthetic import SyntheticCateringAdapter
from .table import TableAdapter

REGISTRY = {
    "hf": HFRecipeAdapter,
    "pdf": MenuPDFAdapter,
    "synthetic": SyntheticCateringAdapter,
    "table": TableAdapter,
}

__all__ = ["HFRecipeAdapter", "MenuPDFAdapter", "SyntheticCateringAdapter", "TableAdapter", "REGISTRY"]
