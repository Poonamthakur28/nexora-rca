"""Removes secrets and personal data from text before it is stored as evidence or sent to an AI.

Conservative by design: it is better to over-redact a harmless string than to store a secret.
"""

import re

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("BEARER", re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}")),
    ("JWT", re.compile(r"\beyJ[a-zA-Z0-9_-]{5,}\.[a-zA-Z0-9_-]{5,}\.[a-zA-Z0-9_-]{5,}")),
    ("AWS_KEY", re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("API_KEY", re.compile(r"\b(sk|pk|rk)[-_](live|test|proj)?[-_]?[A-Za-z0-9]{16,}\b")),
    ("GITHUB_TOKEN", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("SECRET", re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization)\b(\s*[:=]\s*)"
                          r"(['\"]?)[^\s'\",;]{3,}\3")),
    ("CONN_PASSWORD", re.compile(r"(?i)(\w+://[^:/\s@]+:)[^@\s]+(@)")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
]


def redact(text: str | None) -> str:
    if not text:
        return text or ""
    out = text
    for label, pattern in _PATTERNS:
        if label == "SECRET":
            out = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", out)
        elif label == "CONN_PASSWORD":
            out = pattern.sub(lambda m: f"{m.group(1)}[REDACTED]{m.group(2)}", out)
        else:
            out = pattern.sub(f"[REDACTED_{label}]", out)
    return out


def redact_obj(obj):
    """Redacts every string inside nested dicts and lists."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    return obj
