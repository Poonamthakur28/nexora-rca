"""Deterministic identities. Three identities are kept separate and never mixed:

- delivery id:   detects the same webhook body being retried
- external id:   identifies one logical alert at the provider
- fingerprint:   Nexora's grouping key for a logical alert
"""

import hashlib
import json


def delivery_id(body: dict) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def alert_fingerprint(
    organization_id: str, provider: str, integration_id: str, external_alert_id: str,
    service: str | None, environment: str | None,
) -> str:
    material = {
        "v": "alert_fingerprint_v1",
        "organization_id": organization_id,
        "provider": provider,
        "integration_id": integration_id,
        "external_alert_id": external_alert_id,
        "service": service or "",
        "environment": (environment or "").lower(),
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
