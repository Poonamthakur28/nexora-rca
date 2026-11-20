"""Stock reservation. Contains the planted bug `code_inventory_keyerror`."""

from .bugs import is_enabled

STOCK = {"SKU-1": 500, "SKU-2": 200, "SKU-3": 50}


def available_stock(sku: str) -> int:
    if is_enabled("code_inventory_keyerror"):
        # Planted bug: direct indexing raises KeyError for SKUs that are not stocked.
        return STOCK[sku]
    # Correct behaviour: unknown SKUs simply have no stock.
    return STOCK.get(sku, 0)


def reserve_stock(sku: str, quantity: int = 1) -> bool:
    return available_stock(sku) >= quantity
