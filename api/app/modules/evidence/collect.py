"""Runs all collectors for an incident and stores the evidence (the `evidence.collect` job)."""

import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import Evidence, Incident, IncidentEvent
from app.modules.catalog.service import current_deployment, find_service, recent_deployments
from app.modules.pipeline.recorder import PipelineRecorder

from .collectors import (
    CollectionContext,
    EvidenceItem,
    GapError,
    collect_code,
    collect_dependencies,
    collect_deployments,
    collect_logs,
    collect_metrics,
    collect_traces,
)

JOB_TYPE = "evidence.collect"

# (stage, step name, collector). Order matters: code uses the frames found in traces and logs.
COLLECTORS = [
    ("traces", "Traces (Tempo)", collect_traces),
    ("logs", "Logs (Loki)", collect_logs),
    ("metrics", "Metrics (Prometheus)", collect_metrics),
    ("dependencies", "Dependency calls (Tempo)", collect_dependencies),
    ("deployments", "Deployments (catalog)", collect_deployments),
    ("code", "Code (source)", collect_code),
]


def evidence_window_start(session: Session, incident: Incident, settings: Settings,
                          detected: datetime) -> tuple[datetime, str | None]:
    """Look back a fixed amount before detection, but never past the moment an earlier incident on the
    same service cleared: errors from a previous, different failure must not leak into this one."""
    start = detected - timedelta(minutes=settings.evidence_lookback_minutes)
    cleared = session.scalars(
        select(IncidentEvent.occurred_at)
        .join(Incident, Incident.id == IncidentEvent.incident_id)
        .where(Incident.organization_id == incident.organization_id, Incident.id != incident.id,
               Incident.service == incident.service, Incident.environment == incident.environment,
               IncidentEvent.kind == "alerts_cleared", IncidentEvent.occurred_at <= detected)
        .order_by(IncidentEvent.occurred_at.desc()).limit(1)
    ).first()
    if cleared is not None and _as_utc(cleared) > start:
        prev = session.scalars(select(Incident.key).join(IncidentEvent, IncidentEvent.incident_id == Incident.id)
                               .where(IncidentEvent.occurred_at == cleared)).first()
        return _as_utc(cleared), (f"starts {_as_utc(cleared):%H:%M:%S} UTC, when {prev or 'the previous incident'} "
                                  f"cleared, instead of {settings.evidence_lookback_minutes} min before detection")
    return start, None


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def start_collection_run(session: Session, incident: Incident) -> PipelineRecorder:
    return PipelineRecorder.start(session, incident.organization_id, "evidence_collection",
                                  f"Evidence for {incident.key}", trigger_id=str(incident.id))


def collect_evidence(session: Session, settings: Settings, incident_id: str, run_id: str | None = None,
                     http: httpx.Client | None = None) -> dict:
    incident = session.get(Incident, uuid.UUID(incident_id))
    if incident is None:
        raise LookupError(f"incident {incident_id} not found")
    rec = PipelineRecorder.resume(session, uuid.UUID(run_id)) if run_id else None
    if rec is None:
        rec = start_collection_run(session, incident)
    rec.run.incident_id = incident.id
    collection_id = rec.run.id

    service = find_service(session, incident.organization_id, incident.service, incident.environment)
    if service is None:
        rec.step("catalog", "Service found in catalog", "failed",
                 f"'{incident.service}' / '{incident.environment}' is not in the service catalog")
        _gap(session, incident, collection_id, "catalog", "Service not in catalog",
             "Add the service to deploy/config/service-catalog.yaml so Nexora knows its code and telemetry.")
        rec.finish("failed")
        session.commit()
        return {"evidence": 0, "gaps": 1}

    detected = _as_utc(incident.detected_at)
    deployment = current_deployment(session, service, at=detected)
    rec.step("catalog", "Service found in catalog", "success",
             f"{service.name} ({service.environment}), running {deployment.version if deployment else 'unknown version'}")

    window_start, bound_note = evidence_window_start(session, incident, settings, detected)
    rec.step("window", "Evidence window", "success",
             bound_note or f"{settings.evidence_lookback_minutes} min before detection until now")

    owns_client = http is None
    client = http or httpx.Client()
    ctx = CollectionContext(
        settings=settings, http=client, incident=incident, service=service,
        window_start=window_start,
        window_end=datetime.now(timezone.utc), deployment=deployment,
        deployments=recent_deployments(session, service, limit=10),
    )

    stored, gaps = 0, 0
    try:
        for stage, name, collector in COLLECTORS:
            try:
                items: list[EvidenceItem] = collector(ctx)
                for it in items:
                    session.add(Evidence(
                        organization_id=incident.organization_id, incident_id=incident.id,
                        collection_id=collection_id, kind=it.kind, source=it.source, title=it.title[:300],
                        summary=it.summary, content=it.content, relevance=it.relevance, observed_at=it.observed_at,
                    ))
                stored += len(items)
                rec.step(stage, name, "success", f"{len(items)} item(s): " + "; ".join(i.title for i in items)[:400])
            except GapError as gap:
                gaps += 1
                _gap(session, incident, collection_id, stage, f"No {stage} evidence", str(gap))
                rec.step(stage, name, "warning", f"gap: {gap}")
            except Exception as exc:  # the source failed; record it and keep collecting
                gaps += 1
                reason = f"{type(exc).__name__}: {exc}"[:300]
                _gap(session, incident, collection_id, stage, f"{name} unavailable", reason)
                rec.step(stage, name, "failed", reason)
            session.flush()
    finally:
        if owns_client:
            client.close()

    rec.step("stored", "Evidence ranked and stored", "success" if stored else "failed",
             f"{stored} item(s), {gaps} gap(s)")
    if incident.status == "open":
        incident.status = "investigating"
    incident.updated_at = datetime.now(timezone.utc)
    session.add(IncidentEvent(
        organization_id=incident.organization_id, incident_id=incident.id, kind="evidence_collected",
        message=f"Evidence collected: {stored} item(s), {gaps} gap(s).",
        data={"collection_id": str(collection_id)},
    ))
    rec.finish()
    from app.modules.rca.engine import maybe_enqueue_investigation  # local import: rca depends on evidence

    maybe_enqueue_investigation(session, settings, incident, f"new evidence for {incident.key}: "
                                f"{stored} item(s), {gaps} gap(s)")
    session.commit()
    return {"evidence": stored, "gaps": gaps, "collection_id": str(collection_id)}


def _gap(session: Session, incident: Incident, collection_id: uuid.UUID, source: str, title: str,
         reason: str) -> None:
    session.add(Evidence(
        organization_id=incident.organization_id, incident_id=incident.id, collection_id=collection_id,
        kind="gap", source=source, title=title, summary=reason, content={"reason": reason}, relevance=0.0,
    ))


def record_collection_failure(session: Session, run_id: str | None, error: str, final: bool) -> None:
    if not run_id:
        return
    rec = PipelineRecorder.resume(session, uuid.UUID(run_id))
    if rec:
        rec.step("processing", "Evidence collection failed" + ("" if final else ", will retry"), "failed",
                 error[:300])
        if final:
            rec.finish("failed")
    session.commit()
