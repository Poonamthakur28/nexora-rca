"""Checks that need Nexora's own database: PostgreSQL, worker, webhook deliveries, incidents."""

import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.models import AlertDelivery, Evidence, Incident, Job, PipelineRun, WorkerHeartbeat

from .contracts import CheckResult


def _result(check_id, name, category, status, detail, start) -> CheckResult:
    return CheckResult(id=check_id, name=name, category=category, status=status, detail=detail,
                       latency_ms=int((time.perf_counter() - start) * 1000))


def run_db_checks(factory: sessionmaker[Session], settings: Settings) -> tuple[list[CheckResult], list[CheckResult]]:
    org = settings.default_organization_id
    window = datetime.now(timezone.utc) - timedelta(minutes=settings.signal_window_minutes)
    components: list[CheckResult] = []
    signals: list[CheckResult] = []

    start = time.perf_counter()
    try:
        session = factory()
        session.execute(text("SELECT 1"))
    except Exception as exc:
        components.append(_result("postgres", "PostgreSQL (Nexora data)", "component", "failed",
                                  f"{type(exc).__name__}: cannot connect", start))
        skipped = "skipped: database unavailable"
        components.append(_result("worker", "Nexora worker", "component", "skipped", skipped, start))
        signals.append(_result("signal-webhook", "Alerts reach Nexora webhook", "signal", "skipped", skipped, start))
        signals.append(_result("signal-incident", "Incident created", "signal", "skipped", skipped, start))
        signals.append(_result("signal-evidence", "Evidence collected", "signal", "skipped", skipped, start))
        return components, signals

    with session:
        components.append(_result("postgres", "PostgreSQL (Nexora data)", "component", "success", "connected", start))

        start = time.perf_counter()
        beat = session.scalar(select(func.max(WorkerHeartbeat.beat_at)))
        stuck = session.scalar(
            select(func.count()).select_from(Job).where(
                Job.status == "queued", Job.created_at < datetime.now(timezone.utc) - timedelta(seconds=60)
            )
        ) or 0
        if beat is None:
            status, detail = "failed", "no heartbeat yet; is the worker container running?"
        else:
            beat = beat if beat.tzinfo else beat.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - beat).total_seconds()
            if age > 30:
                status, detail = "failed", f"last heartbeat {int(age)} s ago"
            elif stuck:
                status, detail = "warning", f"alive, but {stuck} job(s) waiting over 60 s"
            else:
                status, detail = "success", f"alive (heartbeat {int(age)} s ago)"
        components.append(_result("worker", "Nexora worker", "component", status, detail, start))

        start = time.perf_counter()
        last = session.scalars(
            select(AlertDelivery).where(AlertDelivery.organization_id == org)
            .order_by(AlertDelivery.received_at.desc()).limit(1)
        ).first()
        if last is None:
            signals.append(_result("signal-webhook", "Alerts reach Nexora webhook", "signal", "warning",
                                   "no deliveries yet", start))
        else:
            received = last.received_at if last.received_at.tzinfo else last.received_at.replace(tzinfo=timezone.utc)
            recent = received >= window
            status = {"processed": "success", "failed": "failed"}.get(last.processing_status, "warning")
            if not recent and status == "success":
                status = "warning"
            ago = int((datetime.now(timezone.utc) - received).total_seconds())
            signals.append(_result("signal-webhook", "Alerts reach Nexora webhook", "signal", status,
                                   f"last delivery {ago} s ago, {last.processing_status}", start))

        start = time.perf_counter()
        incident = session.scalars(
            select(Incident).where(Incident.organization_id == org, Incident.service == settings.monitored_service)
            .order_by(Incident.detected_at.desc()).limit(1)
        ).first()
        if incident is None:
            signals.append(_result("signal-incident", "Incident created", "signal", "warning",
                                   f"no incident for {settings.monitored_service} yet", start))
        else:
            signals.append(_result("signal-incident", "Incident created", "signal", "success",
                                   f"{incident.key} {incident.status}, {incident.active_alerts} active alert(s)",
                                   start))

        start = time.perf_counter()
        if incident is None:
            signals.append(_result("signal-evidence", "Evidence collected", "signal", "warning",
                                   "waiting for an incident", start))
        else:
            run = session.scalars(
                select(PipelineRun).where(PipelineRun.incident_id == incident.id,
                                          PipelineRun.trigger_kind == "evidence_collection")
                .order_by(PipelineRun.started_at.desc()).limit(1)
            ).first()
            if run is None:
                signals.append(_result("signal-evidence", "Evidence collected", "signal", "warning",
                                       f"no collection for {incident.key} yet", start))
            elif run.status == "running":
                signals.append(_result("signal-evidence", "Evidence collected", "signal", "warning",
                                       f"collecting for {incident.key}...", start))
            else:
                kinds = [k for (k,) in session.execute(
                    select(Evidence.kind).where(Evidence.collection_id == run.id)).all()]
                found = sorted({k for k in kinds if k != "gap"})
                gaps = kinds.count("gap")
                ok = "code" in found and ("trace" in found or "log" in found)
                status = "success" if ok else ("warning" if found else "failed")
                signals.append(_result("signal-evidence", "Evidence collected", "signal", status,
                                       f"{incident.key}: {', '.join(found) or 'nothing'}; {gaps} gap(s)", start))
    return components, signals
