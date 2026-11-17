"""Planted bugs that can be switched on to create real incidents."""

BUGS: dict[str, dict] = {
    "code_null_tax": {
        "enabled": False,
        "description": "Unknown tax regions return None, so calculate_total raises AttributeError (HTTP 500).",
    },
}


def is_enabled(name: str) -> bool:
    return BUGS.get(name, {}).get("enabled", False)


def set_enabled(name: str, enabled: bool) -> dict:
    if name not in BUGS:
        raise KeyError(name)
    BUGS[name]["enabled"] = enabled
    return {"name": name, **BUGS[name]}
