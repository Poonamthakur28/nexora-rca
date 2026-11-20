"""Watch catalog metrics after an execution, then keep, undo, or escalate."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import utc_now
from app.db.models import (
    Evidence,
    Incident,
    IncidentEvent,
    Job,
    MitigationExecution,
    MitigationPlan,
    RecoveryCheck,
)
from app.jobs.queue import enqueue
from app.modules.catalog.service import find_service
from app.modules.evidence.collectors import metric_specs, prometheus_range
from app.modules.pipeline.recorder import PipelineRecorder

from .adapters import ExecutionResult, adapter_for

JOB_TYPE = "recovery.verify"


def maybe_enqueue_verify(session: Session, settings: Settings, incident: Incident,
                         execution: MitigationExecution) -> Job:
    rec = PipelineRecorder.start(
        session, incident.organization_id, "recovery_verification",
        f"Recovery check for {incident.key}", trigger_id=str(execution.id),
    )
    rec.run.incident_id = incident.id
    rec.step("queued", "Recovery check queued", "success", f"after {execution.plan_id}")
    start = execution.finished_at or utc_now()
    return enqueue(
        session, incident.organization_id, JOB_TYPE,
        {"execution_id": str(execution.id), "incident_id": str(incident.id), "run_id": str(rec.run.id),
         "window_start": start.isoformat()},
    )


def run_verify(session: Session, settings: Settings, execution_id: str, run_id: str | None = None,
               http: httpx.Client | None = None, payload: dict | None = None) -> dict:
    execution = session.get(MitigationExecution, uuid.UUID(execution_id))
    if execution is None:
        raise LookupError(f"execution {execution_id} not found")
    plan = session.get(MitigationPlan, execution.plan_id)
    incident = session.get(Incident, plan.incident_id) if plan else None
    if plan is None or incident is None:
        raise LookupError("execution is missing its plan or incident")
    existing = session.scalars(
        select(RecoveryCheck).where(
            RecoveryCheck.organization_id == execution.organization_id,
            RecoveryCheck.execution_id == execution.id,
        )
    ).first()
    if existing is not None:
        return {"status": existing.verdict, "check_id": str(existing.id)}
    rec = PipelineRecorder.resume(session, uuid.UUID(run_id)) if run_id else None
    if rec is None:
        rec = PipelineRecorder.start(session, incident.organization_id, "recovery_verification",
                                     f"Recovery check for {incident.key}", trigger_id=str(execution.id))
    rec.run.incident_id = incident.id

    window = max(0, int(settings.recovery_window_seconds))
    started = _parse_ts((payload or {}).get("window_start")) or execution.finished_at or utc_now()
    until = started + timedelta(seconds=window)
    now = utc_now()
    if now < until:
        if not _has_stage(rec, "waiting"):
            rec.step("waiting", "Recovery window", "success",
                     f"watching until {until.strftime('%H:%M:%S')} UTC")
        _schedule_next(session, incident, execution, rec, until, settings, payload)
        session.commit()
        return {"status": "waiting"}

    if not _has_stage(rec, "waiting"):
        rec.step("waiting", "Recovery window", "success", f"{window}s window elapsed")

    owns = http is None
    client = http or httpx.Client()
    try:
        signals, readable = _read_signals(session, settings, incident, execution, client, started, now)
        rec.step("signals", "Catalog metrics", "success" if readable else "warning",
                 f"{len(signals)} signal(s)" if readable else "Prometheus did not return metrics")
        alerts_clear = incident.active_alerts == 0
        if not readable:
            verdict = "inconclusive"
        elif all(s["ok"] for s in signals) and alerts_clear:
            verdict = "healthy"
        else:
            verdict = "unhealthy"
        check = RecoveryCheck(
            organization_id=incident.organization_id, execution_id=execution.id,
            window_seconds=window, signals=signals, verdict=verdict,
        )
        session.add(check)
        rec.step("verdict", "Recovery verdict", "success" if verdict == "healthy" else "warning",
                 verdict + ("" if alerts_clear else "; alerts still active"))
        if verdict == "healthy":
            plan.status = "succeeded"
            incident.status = "mitigated"
            _event(session, incident, "mitigation_recovered", f"Recovered after {plan.action_type}")
            rec.finish()
            session.commit()
            return {"status": "healthy", "check_id": str(check.id)}
        if verdict == "inconclusive":
            _event(session, incident, "recovery_inconclusive",
                   "Recovery check inconclusive; metrics unreadable. Escalate to on-call.")
            rec.finish("warning")
            session.commit()
            return {"status": "inconclusive", "check_id": str(check.id)}
        undo = _undo(session, settings, plan, incident, execution, client)
        rec.step("undo", "Undo execution", "success" if undo.ok else "failed", undo.message)
        execution.status = "undone"
        execution.result = {**(execution.result or {}), "undo": undo.result}
        plan.status = "failed"
        incident.status = "investigating"
        _event(session, incident, "mitigation_undone", f"Not recovered; {plan.action_type} undone")
        from app.modules.rca.engine import maybe_enqueue_investigation

        maybe_enqueue_investigation(session, settings, incident,
                                    f"not recovered after {plan.action_type}; investigating again")
        rec.finish("warning")
        session.commit()
        return {"status": "unhealthy", "check_id": str(check.id), "undone": undo.ok}
    finally:
        if owns:
            client.close()


def record_verify_failure(session: Session, run_id: str | None, error: str, final: bool) -> None:
    if not run_id:
        return
    rec = PipelineRecorder.resume(session, uuid.UUID(run_id))
    if rec:
        rec.step("processing", "Recovery check failed" + ("" if final else ", will retry"), "failed", error[:300])
        if final:
            rec.finish("failed")
    session.commit()


def _read_signals(session: Session, settings: Settings, incident: Incident, execution: MitigationExecution,
                  http: httpx.Client, start: datetime, end: datetime) -> tuple[list[dict], bool]:
    service = find_service(session, incident.organization_id, incident.service, incident.environment)
    if service is None:
        return [], False
    specs = {sp["name"]: sp for sp in metric_specs(service)}
    required = _breached_metrics(session, incident)
    if not required:
        required = [{"metric": sp["name"], "threshold": float(sp["threshold"]), "before": None}
                    for sp in specs.values()]
    signals, readable = [], True
    for item in required:
        spec = specs.get(item["metric"])
        if spec is None:
            continue
        points = prometheus_range(http, settings, spec["query"], start, end)
        if points is None:
            readable = False
            signals.append({**item, "after": None, "ok": False, "threshold": float(spec["threshold"])})
            continue
        if not points:
            readable = False
            signals.append({**item, "after": None, "ok": False, "threshold": float(spec["threshold"])})
            continue
        after = points[-1][1]
        threshold = float(item.get("threshold") if item.get("threshold") is not None else spec["threshold"])
        signals.append({
            "metric": spec["name"], "threshold": threshold,
            "before": item.get("before"), "after": round(after, 4),
            "ok": after < threshold,
        })
    if not signals:
        readable = False
    return signals, readable


def _breached_metrics(session: Session, incident: Incident) -> list[dict]:
    rows = session.scalars(
        select(Evidence).where(
            Evidence.organization_id == incident.organization_id,
            Evidence.incident_id == incident.id,
            Evidence.kind == "metric",
        ).order_by(Evidence.collected_at.desc())
    ).all()
    latest = rows[0].collection_id if rows else None
    out = []
    for row in rows:
        if latest is not None and row.collection_id != latest:
            continue
        content = row.content or {}
        if not content.get("breached"):
            continue
        out.append({
            "metric": content.get("metric"),
            "threshold": content.get("threshold"),
            "before": content.get("peak"),
        })
    return [s for s in out if s.get("metric")]


def _undo(session: Session, settings: Settings, plan: MitigationPlan, incident: Incident,
          execution: MitigationExecution, http: httpx.Client) -> ExecutionResult:
    adapter = adapter_for(plan.action_type, session, settings, http)
    if adapter is None:
        return ExecutionResult(ok=False, action_type=plan.action_type, message=f"no adapter for {plan.action_type}")
    return adapter.undo(ExecutionResult(
        ok=True, action_type=plan.action_type, message="undo after failed recovery",
        before_state=execution.before_state or {}, result=execution.result or {},
    ))


def _schedule_next(session: Session, incident: Incident, execution: MitigationExecution,
                   rec: PipelineRecorder, until: datetime, settings: Settings, payload: dict | None) -> Job | None:
    pending = session.scalars(
        select(Job).where(
            Job.organization_id == incident.organization_id,
            Job.type == JOB_TYPE,
            Job.status == "queued",
        )
    ).all()
    if any(j.payload.get("execution_id") == str(execution.id) for j in pending):
        return None
    delay = max(0, int(settings.recovery_poll_seconds))
    run_after = min(utc_now() + timedelta(seconds=delay), until)
    body = {
        "execution_id": str(execution.id), "incident_id": str(incident.id), "run_id": str(rec.run.id),
        "window_start": (payload or {}).get("window_start") or (execution.finished_at or utc_now()).isoformat(),
    }
    return enqueue(session, incident.organization_id, JOB_TYPE, body, run_after=run_after)


def _has_stage(rec: PipelineRecorder, stage: str) -> bool:
    return any(s.stage == stage for s in rec.run.steps)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _event(session: Session, incident: Incident, kind: str, message: str) -> None:
    incident.updated_at = utc_now()
    session.add(IncidentEvent(organization_id=incident.organization_id, incident_id=incident.id,
                              kind=kind, message=message[:1000]))
