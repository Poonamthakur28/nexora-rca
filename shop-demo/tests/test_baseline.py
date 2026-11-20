"""Baseline behaviour of shop-demo's business logic, with every planted failure switched off.

Nexora's fix track (Step 6) runs these before and after a proposed patch: a fix must keep them passing.
They import only application modules, so they run without the web server or telemetry.
"""

import pytest

from app import bugs
from app.inventory import available_stock, reserve_stock
from app.orders import POOL, save_order
from app.pricing import TaxRule, calculate_total, find_tax_rule


@pytest.fixture(autouse=True)
def all_bugs_off():
    saved = {name: info["enabled"] for name, info in bugs.BUGS.items()}
    for name in bugs.BUGS:
        bugs.set_enabled(name, False)
    yield
    for name, enabled in saved.items():
        bugs.set_enabled(name, enabled)


def test_known_region_uses_its_tax_rule():
    assert find_tax_rule("DL") == TaxRule("DL", 0.12)
    assert calculate_total(100.0, "DL") == {"subtotal": 100.0, "tax": 12.0, "total": 112.0}


def test_unknown_region_falls_back_to_default_rate():
    assert calculate_total(100.0, "TN") == {"subtotal": 100.0, "tax": 18.0, "total": 118.0}


def test_stock_for_known_and_unknown_skus():
    assert available_stock("SKU-3") == 50
    assert available_stock("SKU-404") == 0
    assert reserve_stock("SKU-1") is True
    assert reserve_stock("SKU-404") is False


def test_saving_orders_returns_connections_to_the_pool():
    POOL.reset()
    for _ in range(POOL.size + 2):  # more saves than connections: only works if each one is returned
        assert save_order({"total": 1.0}).startswith("order-")
    assert POOL.in_use == 0
