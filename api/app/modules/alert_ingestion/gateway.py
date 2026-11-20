"""Alertmanager webhook gateway.

Order of work, all visible in the Pipeline view:
  received -> authenticated -> validated -> stored (+ job queued, one transaction) -> 202
The organization always comes from the verified integration, never from the payload.
"""

import json
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import AlertDelivery, AlertIntegration
from app.jobs.queue import enqueue
from app.modules.pipeline.recorder import PipelineRecorder

from .alertmanager import PROVIDER, AMPayload
from .fingerprint import delivery_id, token_hash

UNAUTHENTICATED_ORG = "unauthenticated"
JOB_TYPE = "alert.process"


@dataclass
class GatewayResult:
    status_code: int
    body: dict


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def find_integration(session: Session, token: str | None) -> AlertIntegration | None:
    if not token:
        return None
    return session.scalars(
        select(AlertIntegration).where(
            AlertIntegration.provider == PROVIDER,
            AlertIntegration.token_sha256 == token_hash(token),
            AlertIntegration.enabled.is_(True),
        )
    ).first()


def receive_alertmanager(session: Session, settings: Settings, authorization: str | None,
                         raw_body: bytes) -> GatewayResult:
    try:
        return _receive(session, settings, authorization, raw_body)
    except OperationalError:
        session.rollback()
        # Database unavailable: tell Alertmanager to retry later.
        return GatewayResult(503, {"status": "unavailable", "detail": "storage unavailable, retry later"})


def _receive(session: Session, settings: Settings, authorization: str | None, raw_body: bytes) -> GatewayResult:
    size = len(raw_body)

    # 1. Authenticate first, so nothing is stored for unknown senders beyond a failure record.
    integration = find_integration(session, _bearer(authorization))
    if integration is None:
        rec = PipelineRecorder.start(session, UNAUTHENTICATED_ORG, "alert_delivery", "Rejected webhook")
        rec.step("received", "Webhook received", "success", f"{size} bytes from Alertmanager endpoint")
        rec.step("authenticated", "Sender authenticated", "failed", "missing or invalid bearer token")
        rec.finish("failed")
        session.commit()
        return GatewayResult(401, {"status": "unauthorized"})

    org = integration.organization_id
    rec = PipelineRecorder.start(session, org, "alert_delivery", "Alertmanager webhook")
    rec.step("received", "Webhook received", "success", f"{size} bytes")
    rec.step("authenticated", "Sender authenticated", "success", f"integration '{integration.name}'")

    # 2. Validate size and shape.
    if size > settings.webhook_max_body_bytes:
        rec.step("validated", "Payload validated", "failed", f"body too large ({size} bytes)")
        rec.finish("failed")
        session.commit()
        return GatewayResult(413, {"status": "too_large"})
    try:
        body = json.loads(raw_body)
        payload = AMPayload.model_validate(body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
        reason = "invalid JSON" if not isinstance(exc, ValidationError) else f"{exc.error_count()} schema error(s)"
        rec.step("validated", "Payload validated", "failed", reason)
        rec.finish("failed")
        session.commit()
        return GatewayResult(422, {"status": "invalid", "detail": reason})

    names = sorted({a.labels.get("alertname", "?") for a in payload.alerts})
    rec.run.title = f"{', '.join(names)} ({payload.status})"
    rec.step("validated", "Payload validated", "success",
             f"{len(payload.alerts)} alert(s), group status {payload.status}")

    # 3. Idempotency: an identical body is the same delivery retried.
    ext_id = delivery_id(body)
    existing = session.scalars(
        select(AlertDelivery).where(
            AlertDelivery.organization_id == org,
            AlertDelivery.integration_id == integration.id,
            AlertDelivery.external_delivery_id == ext_id,
        )
    ).first()
    if existing is not None:
        rec.step("stored", "Delivery stored", "warning", "duplicate delivery; acknowledged, nothing created")
        rec.finish("warning")
        session.commit()
        return GatewayResult(200, {"status": "duplicate", "delivery_id": str(existing.id)})

    # 4. Persist delivery and enqueue processing in the same transaction.
    delivery = AlertDelivery(
        organization_id=org, integration_id=integration.id, provider=PROVIDER,
        external_delivery_id=ext_id, raw_payload=body, alert_count=len(payload.alerts),
        processing_status="received",
    )
    session.add(delivery)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return GatewayResult(200, {"status": "duplicate"})
    rec.run.trigger_id = str(delivery.id)
    rec.step("stored", "Delivery stored", "success", f"delivery {str(delivery.id)[:8]}")
    job = enqueue(session, org, JOB_TYPE, {"delivery_id": str(delivery.id), "run_id": str(rec.run.id)})
    rec.step("queued", "Processing job queued", "success", f"job {str(job.id)[:8]}")
    session.commit()
    # Accepted means stored and queued, not that an incident exists yet.
    return GatewayResult(202, {"status": "accepted", "delivery_id": str(delivery.id), "run_id": str(rec.run.id)})
