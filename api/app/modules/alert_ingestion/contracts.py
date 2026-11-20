"""Provider-neutral alert contracts (the CanonicalAlert from Phase 1.3a, trimmed for this build)."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

CanonicalSeverity = Literal["critical", "high", "medium", "low", "info", "unknown"]
CanonicalStatus = Literal["firing", "acknowledged", "resolved", "unknown"]

FORBIDDEN_ATTRIBUTE_KEYS = {"password", "secret", "token", "api_key", "apikey", "authorization", "credential"}


class CanonicalAlert(BaseModel):
    provider: str
    external_alert_id: str
    title: str
    description: str | None = None
    service: str | None = None
    environment: str | None = None
    provider_severity: str | None = None
    normalized_severity: CanonicalSeverity
    provider_status: str | None = None
    normalized_status: CanonicalStatus
    started_at: datetime
    resolved_at: datetime | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        v = " ".join(v.split())
        return v[:300] or "Untitled alert"

    @field_validator("attributes")
    @classmethod
    def _no_secrets(cls, v: dict[str, Any]) -> dict[str, Any]:
        def scrub(obj):
            if isinstance(obj, dict):
                return {
                    k: ("***" if str(k).lower().replace("-", "_") in FORBIDDEN_ATTRIBUTE_KEYS else scrub(val))
                    for k, val in obj.items()
                }
            return obj

        return scrub(v)
