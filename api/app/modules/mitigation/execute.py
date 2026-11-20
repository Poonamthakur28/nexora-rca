"""Run one approved catalog action. Never retried automatically (max_attempts=1)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import utc_now
from app.db.models import Incident, IncidentEvent, Job, MitigationExecution, MitigationPlan
from app.jobs.queue import enqueue
from app.modules.pipeline.recorder import PipelineRecorder

from .adapters import ActionPlan, adapter_for

JOB_TYPE = "mitigation.execute"


def maybe_enqueue_execute(session: Session, settings: Settings, plan: MitigationPlan, incident: Incident,
                          actor: str) -> Job:
    rec = PipelineRecorder.start(
        session, incident.organization_id, "mitigation_execution",
        f"Execute {plan.action_type} for {incident.key}", trigger_id=str(plan.id),
    )
    rec.run.incident_id = incident.id
    rec.step("queued", "Execution queued", "success", f"approved by {actor}")
    return enqueue(
        session, incident.organization_id, JOB_TYPE,
        {"plan_id": str(plan.id), "incident_id": str(incident.id), "run_id": str(rec.run.id), "actor": actor},
        max_attempts=1,
    )


def run_execute(session: Session, settings: Settings, plan_id: str, run_id: str | None = None,
                actor: str = "local-user", http: httpx.Client | None = None) -> dict:
    plan = session.get(MitigationPlan, uuid.UUID(plan_id))
    if plan is None:
        raise LookupError(f"plan {plan_id} not found")
    incident = session.get(Incident, plan.incident_id)
    if incident is None:
        raise LookupError(f"incident {plan.incident_id} not found")
    rec = PipelineRecorder.resume(session, uuid.UUID(run_id)) if run_id else None
    if rec is None:
        rec = PipelineRecorder.start(
            session, plan.organization_id, "mitigation_execution",
            f"Execute {plan.action_type} for {incident.key}", trigger_id=str(plan.id),
        )
    rec.run.incident_id = incident.id
    if plan.status not in ("approved", "executing"):
        rec.step("lock", "Execution lock", "failed", f"plan is {plan.status}, not approved")
        rec.finish("failed")
        session.commit()
        return {"status": "skipped", "error": f"plan is {plan.status}"}

    execution, lock_error = _acquire_lock(session, plan, incident)
    if execution is None:
        plan.status = "failed"
        _event(session, incident, "mitigation_failed",
               f"Could not execute {plan.action_type}: {lock_error}")
        rec.step("lock", "Execution lock", "failed", lock_error or "lock denied")
        rec.finish("failed")
        session.commit()
        return {"status": "failed", "error": lock_error}

    plan.status = "executing"
    rec.step("lock", "Execution lock", "success",
             f"locked {execution.service}/{execution.environment}")

    owns = http is None
    client = http or httpx.Client()
    try:
        adapter = adapter_for(plan.action_type, session, settings, client)
        if adapter is None:
            return _fail(session, rec, plan, incident, execution, actor,
                         "preflight", f"no adapter for {plan.action_type}")
        action = ActionPlan(
            organization_id=plan.organization_id,
            service=incident.service or "",
            environment=incident.environment or "local",
            params=plan.params or {},
        )
        preflight = adapter.preflight(action)
        if not preflight.ok:
            rec.step("preflight", "Preflight", "failed", preflight.detail)
            return _fail(session, rec, plan, incident, execution, actor, "preflight", preflight.detail)

        rec.step("preflight", "Preflight", "success", preflight.detail)
        result = adapter.execute(action)
        execution = session.get(MitigationExecution, execution.id) or execution
        plan = session.get(MitigationPlan, plan.id) or plan
        incident = session.get(Incident, incident.id) or incident
        execution.before_state = result.before_state
        execution.result = result.result
        if not result.ok:
            rec.step("execute", "Adapter execute", "failed", result.message)
            return _fail(session, rec, plan, incident, execution, actor, "execute",
                         (result.error.message if result.error else result.message),
                         before_state=result.before_state, result=result.result)

        rec.step("execute", "Adapter execute", "success", result.message)
        execution.status = "succeeded"
        execution.finished_at = utc_now()
        execution.error = None
        plan.status = "succeeded"
        _event(session, incident, "mitigation_executed",
               f"Executed {plan.action_type} (approved by {actor})",
               {"execution_id": str(execution.id), "actor": actor})
        rec.step("stored", "Result stored", "success", result.message)
        rec.finish()
        from app.modules.mitigation.recovery import maybe_enqueue_verify

        maybe_enqueue_verify(session, settings, incident, execution)
        session.commit()
        return {"status": "succeeded", "execution_id": str(execution.id)}
    finally:
        if owns:
            client.close()


def record_execute_failure(session: Session, payload: dict, error: str, final: bool) -> None:
    run_id = payload.get("run_id")
    if run_id:
        rec = PipelineRecorder.resume(session, uuid.UUID(run_id))
        if rec:
            rec.step("processing", "Execution failed", "failed", error[:300])
            rec.finish("failed")
    if final and payload.get("plan_id"):
        plan = session.get(MitigationPlan, uuid.UUID(payload["plan_id"]))
        if plan is not None and plan.status in ("approved", "executing"):
            plan.status = "failed"
        running = session.scalars(
            select(MitigationExecution).where(
                MitigationExecution.plan_id == uuid.UUID(payload["plan_id"]),
                MitigationExecution.status == "running",
            )
        ).first()
        if running is not None:
            running.status = "failed"
            running.error = error[:1000]
            running.finished_at = utc_now()
    session.commit()


def _acquire_lock(session: Session, plan: MitigationPlan,
                  incident: Incident) -> tuple[MitigationExecution | None, str | None]:
    """Insert the running row. PostgreSQL unique index and this check both apply; SQLite uses the check."""
    service, env = incident.service or "", incident.environment or ""
    existing = session.scalars(
        select(MitigationExecution).where(
            MitigationExecution.organization_id == plan.organization_id,
            MitigationExecution.service == service,
            MitigationExecution.environment == env,
            MitigationExecution.status == "running",
        ).limit(1)
    ).first()
    if existing:
        return None, f"an execution is already running for {service}/{env}"
    row = MitigationExecution(
        organization_id=plan.organization_id, plan_id=plan.id, service=service, environment=env,
        status="running",
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        return None, f"an execution is already running for {service}/{env}"
    return row, None


def _fail(session: Session, rec: PipelineRecorder, plan: MitigationPlan, incident: Incident,
          execution: MitigationExecution, actor: str, stage: str, error: str,
          before_state: dict | None = None, result: dict | None = None) -> dict:
    execution.status = "failed"
    execution.error = error[:1000]
    execution.finished_at = utc_now()
    if before_state is not None:
        execution.before_state = before_state
    if result is not None:
        execution.result = result
    plan.status = "failed"
    _event(session, incident, "mitigation_failed",
           f"Execution of {plan.action_type} failed: {error}",
           {"execution_id": str(execution.id), "actor": actor, "stage": stage})
    rec.step("stored", "Result stored", "failed", error[:300])
    rec.finish("failed")
    session.commit()
    return {"status": "failed", "error": error, "execution_id": str(execution.id)}


def _event(session: Session, incident: Incident, kind: str, message: str, data: dict | None = None) -> None:
    incident.updated_at = datetime.now(timezone.utc)
    session.add(IncidentEvent(organization_id=incident.organization_id, incident_id=incident.id,
                              kind=kind, message=message[:1000], data=data or {}))
