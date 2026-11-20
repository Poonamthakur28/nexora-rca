"""Planted failures that can be switched on to create real incidents.

kind:
  code        a bug in shop-demo's own code (the test bench records a release that changed `release_files`)
  dependency  something outside shop-demo misbehaves; no release is recorded, no shop-demo code is at fault
  resource    shop-demo exhausts a resource (here: its database connection pool)
"""

BUGS: dict[str, dict] = {
    "code_null_tax": {
        "enabled": False,
        "kind": "code",
        "release_files": ["app/pricing.py"],
        "description": "Unknown tax regions return None, so calculate_total raises AttributeError (HTTP 500).",
    },
    "code_inventory_keyerror": {
        "enabled": False,
        "kind": "code",
        "release_files": ["app/inventory.py"],
        "description": "Stock lookup indexes the stock table directly, so unknown SKUs raise KeyError (HTTP 500).",
    },
    "slow_payment_gateway": {
        "enabled": False,
        "kind": "dependency",
        "release_files": [],
        "description": "The external payment gateway answers in about 2.5 s instead of 20 ms (slow, no errors).",
    },
    "db_pool_leak": {
        "enabled": False,
        "kind": "resource",
        "release_files": ["app/orders.py"],
        "description": "save_order never returns its connection, so the pool of 5 runs out and checkouts time out.",
    },
}


def is_enabled(name: str) -> bool:
    return BUGS.get(name, {}).get("enabled", False)


def set_enabled(name: str, enabled: bool) -> dict:
    if name not in BUGS:
        raise KeyError(name)
    BUGS[name]["enabled"] = enabled
    return {"name": name, **BUGS[name]}
