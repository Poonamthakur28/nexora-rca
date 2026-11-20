"""Alertmanager webhook adapter: payload validation and normalization to CanonicalAlert.

Everything specific to Alertmanager lives here; the rest of Nexora sees only CanonicalAlert.
Payload shape: https://prometheus.io/docs/alerting/latest/configuration/#webhook_config
"""

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from .contracts import CanonicalAlert
from .mapping import normalize_severity, normalize_status

PROVIDER = "prometheus_alertmanager"
ZERO_TIME_PREFIX = "0001-01-01"  # Alertmanager's "not set" timestamp for endsAt


class AMAlert(BaseModel):
    status: str
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: str
    endsAt: str | None = None
    generatorURL: str | None = None
    fingerprint: str


class AMPayload(BaseModel):
    version: str | None = None
    groupKey: str | None = None
    receiver: str | None = None
    status: str
    alerts: list[AMAlert] = Field(min_length=1, max_length=500)
    groupLabels: dict[str, str] = Field(default_factory=dict)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)
    externalURL: str | None = None


def _parse_time(value: str | None) -> datetime | None:
    if not value or value.startswith(ZERO_TIME_PREFIX):
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def normalize(am: AMAlert, received_at: datetime) -> CanonicalAlert:
    labels, ann = am.labels, am.annotations
    status = normalize_status(PROVIDER, am.status)
    started = _parse_time(am.startsAt) or received_at
    resolved = _parse_time(am.endsAt) if status == "resolved" else None
    title = ann.get("summary") or labels.get("alertname") or "Alertmanager alert"
    return CanonicalAlert(
        provider=PROVIDER,
        external_alert_id=am.fingerprint,
        title=title,
        description=ann.get("description"),
        service=labels.get("service") or labels.get("job"),
        environment=labels.get("environment") or labels.get("env"),
        provider_severity=labels.get("severity"),
        normalized_severity=normalize_severity(PROVIDER, labels.get("severity")),
        provider_status=am.status,
        normalized_status=status,
        started_at=started,
        resolved_at=resolved,
        attributes={
            "alertname": labels.get("alertname"),
            "labels": labels,
            "annotations": ann,
            "generator_url": am.generatorURL,
        },
    )
