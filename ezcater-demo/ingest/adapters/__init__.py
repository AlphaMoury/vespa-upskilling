"""Source adapters. Each turns one heterogeneous source into MenuItems.

Register new adapters here so run_ingest.py can look them up by name.
"""

from .hf import HFRecipeAdapter
from .pdf import MenuPDFAdapter
from .synthetic import SyntheticCateringAdapter

REGISTRY = {
    "hf": HFRecipeAdapter,
    "pdf": MenuPDFAdapter,
    "synthetic": SyntheticCateringAdapter,
}

__all__ = ["HFRecipeAdapter", "MenuPDFAdapter", "SyntheticCateringAdapter", "REGISTRY"]
