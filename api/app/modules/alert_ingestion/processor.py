"""Worker handler for `alert.process`: normalize, store alerts, create or link incidents."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import Alert, AlertDelivery, AlertIncidentLink, Incident, IncidentEvent
from app.jobs.queue import enqueue
from app.modules.evidence.collect import JOB_TYPE as EVIDENCE_JOB
from app.modules.evidence.collect import start_collection_run
from app.modules.pipeline.recorder import PipelineRecorder

from .alertmanager import AMPayload, normalize
from .contracts import CanonicalAlert
from .fingerprint import alert_fingerprint
from .mapping import max_severity

OPEN_STATES = ("open", "investigating", "mitigated", "fixing", "escalated")


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event(session: Session, incident: Incident, kind: str, message: str, data: dict | None = None) -> None:
    session.add(IncidentEvent(organization_id=incident.organization_id, incident_id=incident.id,
                              kind=kind, message=message, data=data or {}))


def _upsert_alert(session: Session, delivery: AlertDelivery, ca: CanonicalAlert) -> tuple[Alert, str]:
    org, now = delivery.organization_id, _now()
    alert = session.scalars(
        select(Alert).where(
            Alert.organization_id == org,
            Alert.integration_id == delivery.integration_id,
            Alert.external_alert_id == ca.external_alert_id,
        )
    ).first()
    fields = dict(
        title=ca.title, description=ca.description, service=ca.service, environment=ca.environment,
        provider_severity=ca.provider_severity, normalized_severity=ca.normalized_severity,
        provider_status=ca.provider_status, normalized_status=ca.normalized_status,
        resolved_at=ca.resolved_at, last_seen_at=now, attributes=ca.attributes,
    )
    if alert is None:
        alert = Alert(
            organization_id=org, integration_id=delivery.integration_id, provider=ca.provider,
            external_alert_id=ca.external_alert_id, started_at=ca.started_at, received_at=now,
            fingerprint=alert_fingerprint(org, ca.provider, str(delivery.integration_id), ca.external_alert_id,
                                          ca.service, ca.environment),
            **fields,
        )
        session.add(alert)
        session.flush()
        return alert, "new"
    was = alert.normalized_status
    alert.previous_resolved_at = alert.resolved_at  # transient: used by _link to tell a flap from a recurrence
    for k, v in fields.items():
        setattr(alert, k, v)
    if ca.normalized_status == "firing" and was == "resolved":
        alert.started_at = ca.started_at  # re-fired
    alert.version += 1
    session.flush()
    if was != ca.normalized_status:
        return alert, ca.normalized_status
    return alert, "unchanged"


def _next_key(session: Session) -> str:
    count = session.scalar(select(func.count()).select_from(Incident)) or 0
    return f"INC-{1001 + count}"


def _refresh_active_count(session: Session, incident: Incident) -> int:
    active = session.scalar(
        select(func.count())
        .select_from(AlertIncidentLink)
        .join(Alert, Alert.id == AlertIncidentLink.alert_id)
        .where(AlertIncidentLink.incident_id == incident.id, AlertIncidentLink.ended_at.is_(None),
               Alert.normalized_status == "firing")
    ) or 0
    incident.active_alerts = active
    incident.updated_at = _now()
    return active


def _link(session: Session, settings: Settings, alert: Alert, change: str) -> tuple[Incident | None, str]:
    org = alert.organization_id
    linked = session.scalars(
        select(Incident)
        .join(AlertIncidentLink, AlertIncidentLink.incident_id == Incident.id)
        .where(AlertIncidentLink.alert_id == alert.id, AlertIncidentLink.ended_at.is_(None),
               Incident.status.in_(OPEN_STATES))
        .order_by(Incident.detected_at.desc())
    ).first()

    if alert.normalized_status == "resolved":
        if linked is None:
            return None, f"'{alert.title}' resolved; no open incident linked"
        _event(session, linked, "alert_resolved", f"Alert resolved: {alert.title}", {"alert_id": str(alert.id)})
        active = _refresh_active_count(session, linked)
        if active == 0:
            _event(session, linked, "alerts_cleared",
                   "All linked alerts resolved. Incident stays open until recovery is verified.")
        return linked, f"alert resolved on {linked.key}; {active} active alert(s) left"

    if linked is not None and change == "firing":
        # The same alert firing again is a flap (same incident) only if the incident is still active or the
        # alert cleared moments ago. Otherwise it is a recurrence: a new incident with fresh evidence.
        cleared = getattr(alert, "previous_resolved_at", None)
        window = timedelta(minutes=settings.alert_reopen_window_minutes)
        flap = linked.active_alerts > 0 or (cleared is not None and _now() - _aware(cleared) <= window)
        if flap:
            _event(session, linked, "alert_refired", f"Alert firing again: {alert.title}")
            _refresh_active_count(session, linked)
            return linked, f"already linked to {linked.key} (alert firing again)"
        _event(session, linked, "alert_recurred",
               f"Alert fired again after the incident cleared; it moves to a new incident: {alert.title}")
        # The old link is ended, not deleted: the old incident keeps its alert history.
        for link in session.scalars(select(AlertIncidentLink).where(AlertIncidentLink.alert_id == alert.id,
                                                                    AlertIncidentLink.ended_at.is_(None))):
            link.ended_at = _now()
        session.flush()
        linked = None
    elif linked is not None:
        _refresh_active_count(session, linked)
        return linked, f"already linked to {linked.key}"

    # Correlation rule: a new alert joins an open incident for the same service and environment only
    # while that incident still has active alerts. Once everything has cleared, a new alert is a new incident.
    candidate = session.scalars(
        select(Incident).where(
            Incident.organization_id == org,
            Incident.status.in_(OPEN_STATES),
            Incident.service == alert.service,
            Incident.environment == alert.environment,
            Incident.active_alerts > 0,
        ).order_by(Incident.detected_at.desc())
    ).first()

    if candidate is not None:
        session.add(AlertIncidentLink(organization_id=org, alert_id=alert.id, incident_id=candidate.id,
                                      reason="same service and environment, incident open"))
        candidate.severity = max_severity(candidate.severity, alert.normalized_severity)
        _event(session, candidate, "alert_linked", f"Alert linked: {alert.title}", {"alert_id": str(alert.id)})
        session.flush()
        _refresh_active_count(session, candidate)
        return candidate, f"linked to existing {candidate.key} (new alert)"

    incident = Incident(
        organization_id=org, key=_next_key(session), title=alert.title, service=alert.service,
        environment=alert.environment, severity=alert.normalized_severity, status="open",
        detected_at=alert.started_at,
    )
    session.add(incident)
    session.flush()
    session.add(AlertIncidentLink(organization_id=org, alert_id=alert.id, incident_id=incident.id,
                                  reason="first alert for this service and environment"))
    _event(session, incident, "incident_opened",
           f"Incident opened from alert: {alert.title}",
           {"alert_id": str(alert.id), "service": alert.service, "environment": alert.environment})
    session.flush()
    _refresh_active_count(session, incident)
    return incident, f"created {incident.key}"


def process_delivery(session: Session, settings: Settings, delivery_id: str, run_id: str | None) -> dict:
    delivery = session.get(AlertDelivery, uuid.UUID(delivery_id))
    if delivery is None:
        raise LookupError(f"delivery {delivery_id} not found")
    rec = PipelineRecorder.resume(session, uuid.UUID(run_id)) if run_id else None
    delivery.processing_status = "processing"

    payload = AMPayload.model_validate(delivery.raw_payload)
    canonical = [normalize(a, delivery.received_at) for a in payload.alerts]
    if rec:
        summary = "; ".join(f"{c.attributes.get('alertname')} {c.normalized_status} {c.normalized_severity}"
                            for c in canonical)
        rec.step("normalized", "Normalized to CanonicalAlert", "success", summary)

    changes: dict[str, int] = {}
    stored: list[tuple[Alert, str]] = []
    for ca in canonical:
        alert, change = _upsert_alert(session, delivery, ca)
        stored.append((alert, change))
        changes[change] = changes.get(change, 0) + 1
    if rec:
        rec.step("alerts_stored", "Alerts stored", "success",
                 ", ".join(f"{v} {k}" for k, v in sorted(changes.items())))

    outcomes, incident_ids, created = [], [], []
    for alert, change in stored:
        incident, outcome = _link(session, settings, alert, change)
        outcomes.append(outcome)
        if incident is not None:
            incident_ids.append(incident.id)
            if outcome.startswith("created") or outcome.endswith("(new alert)"):
                created.append(incident)
    if rec:
        rec.step("incident_linked", "Incident created or linked", "success", "; ".join(outcomes))
        if incident_ids:
            rec.run.incident_id = incident_ids[0]
        rec.finish()

    # A new incident, or a new kind of alert joining one, gets evidence collected straight away.
    for incident in {i.id: i for i in created}.values():
        erec = start_collection_run(session, incident)
        erec.run.incident_id = incident.id
        erec.step("queued", "Collection job queued", "success", f"new alert on {incident.key}")
        enqueue(session, incident.organization_id, EVIDENCE_JOB, {"incident_id": str(incident.id),
                                                                  "run_id": str(erec.run.id)}, max_attempts=3)

    delivery.processing_status = "processed"
    delivery.processing_error = None
    session.commit()
    return {"alerts": len(stored), "incidents": [str(i) for i in incident_ids]}


def record_processing_failure(session: Session, delivery_id: str, run_id: str | None, error: str,
                              final: bool) -> None:
    delivery = session.get(AlertDelivery, uuid.UUID(delivery_id))
    if delivery is not None:
        delivery.processing_status = "failed" if final else "received"
        delivery.processing_error = error[:1000]
    if run_id:
        rec = PipelineRecorder.resume(session, uuid.UUID(run_id))
        if rec:
            label = "Processing failed" if final else "Processing failed, will retry"
            rec.step("processing", label, "failed", error[:300])
            if final:
                rec.finish("failed")
    session.commit()
