"""Provider severity/status -> Nexora values. Unknown or missing values become 'unknown', never 'critical'."""

SEVERITY_MAP: dict[str, dict[str, str]] = {
    "prometheus_alertmanager": {
        "critical": "critical",
        "page": "critical",
        "error": "high",
        "high": "high",
        "warning": "medium",
        "warn": "medium",
        "medium": "medium",
        "low": "low",
        "info": "info",
        "none": "info",
    },
}

STATUS_MAP: dict[str, dict[str, str]] = {
    "prometheus_alertmanager": {"firing": "firing", "resolved": "resolved"},
}


def normalize_severity(provider: str, value: str | None) -> str:
    if not value or not value.strip():
        return "unknown"
    return SEVERITY_MAP.get(provider, {}).get(value.strip().lower(), "unknown")


def normalize_status(provider: str, value: str | None) -> str:
    if not value or not value.strip():
        return "unknown"
    return STATUS_MAP.get(provider, {}).get(value.strip().lower(), "unknown")


SEVERITY_ORDER = ["unknown", "info", "low", "medium", "high", "critical"]


def max_severity(a: str, b: str) -> str:
    return a if SEVERITY_ORDER.index(a) >= SEVERITY_ORDER.index(b) else b
