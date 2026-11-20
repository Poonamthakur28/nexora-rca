import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.identity import get_actor, get_organization_id
from app.db.base import get_db
from app.db.models import (
    Incident,
    IncidentEvent,
    MitigationExecution,
    MitigationPlan,
    PipelineRun,
    RecoveryCheck,
)

from .execute import maybe_enqueue_execute
from .policy import recheck_gates

router = APIRouter(prefix="/api/v1", tags=["mitigation"])


class DecisionBody(BaseModel):
    comment: str = Field(default="", max_length=1000)


CHOSEN_STATUSES = {"proposed", "approved", "executing", "succeeded", "failed", "undone", "rejected"}
PENDING_PLAN = {"approved", "executing"}
PIPELINE_KINDS = ("mitigation_plan", "mitigation_execution", "recovery_verification")


def _target(p: MitigationPlan, incident: Incident | None) -> str:
    params = p.params or {}
    if params.get("flag"):
        return str(params["flag"])
    if incident and incident.service:
        return incident.service + (f" · {incident.environment}" if incident.environment else "")
    return ""


def _plan_out(p: MitigationPlan, incident: Incident | None = None) -> dict:
    return {
        "id": str(p.id), "incident_id": str(p.incident_id),
        "rca_report_id": str(p.rca_report_id) if p.rca_report_id else None,
        "action_type": p.action_type, "params": p.params, "title": p.title, "risk": p.risk,
        "approval_mode": p.approval_mode, "gate_results": p.gate_results, "score": p.score,
        "status": p.status, "rationale": p.rationale, "undo_plan": p.undo_plan,
        "target": _target(p, incident),
        "decided_by": p.decided_by, "decided_at": p.decided_at, "created_at": p.created_at,
    }


def _check_out(c: RecoveryCheck) -> dict:
    return {
        "id": str(c.id), "execution_id": str(c.execution_id), "window_seconds": c.window_seconds,
        "signals": c.signals, "verdict": c.verdict, "checked_at": c.checked_at,
    }


def _latest_check(db: Session, org: str, execution_id) -> RecoveryCheck | None:
    return db.scalars(
        select(RecoveryCheck).where(
            RecoveryCheck.organization_id == org, RecoveryCheck.execution_id == execution_id,
        ).order_by(RecoveryCheck.checked_at.desc())
    ).first()


def _exec_out(e: MitigationExecution, recovery: RecoveryCheck | None = None) -> dict:
    return {
        "id": str(e.id), "plan_id": str(e.plan_id), "service": e.service, "environment": e.environment,
        "status": e.status, "before_state": e.before_state, "result": e.result, "error": e.error,
        "started_at": e.started_at, "finished_at": e.finished_at,
        "recovery": _check_out(recovery) if recovery else None,
    }


def _plan_for_org(db: Session, org: str, plan_id: uuid.UUID) -> MitigationPlan:
    plan = db.scalars(
        select(MitigationPlan).where(MitigationPlan.id == plan_id, MitigationPlan.organization_id == org)
    ).first()
    if plan is None:
        raise HTTPException(404, "plan not found")
    return plan


@router.get("/incidents/{incident_id}/mitigation-plans")
def list_plans(incident_id: uuid.UUID, org: str = Depends(get_organization_id), db: Session = Depends(get_db)):
    inc = db.scalars(select(Incident).where(Incident.id == incident_id, Incident.organization_id == org)).first()
    if inc is None:
        raise HTTPException(404, "incident not found")
    rows = db.scalars(
        select(MitigationPlan).where(MitigationPlan.incident_id == inc.id, MitigationPlan.organization_id == org)
        .order_by(MitigationPlan.score.desc(), MitigationPlan.created_at.desc())
    ).all()
    return [_plan_out(p, inc) for p in rows]


@router.get("/incidents/{incident_id}/mitigation")
def get_mitigation(incident_id: uuid.UUID, org: str = Depends(get_organization_id), db: Session = Depends(get_db)):
    inc = db.scalars(select(Incident).where(Incident.id == incident_id, Incident.organization_id == org)).first()
    if inc is None:
        raise HTTPException(404, "incident not found")
    plans = db.scalars(
        select(MitigationPlan).where(MitigationPlan.incident_id == inc.id, MitigationPlan.organization_id == org)
        .order_by(MitigationPlan.created_at.desc(), MitigationPlan.score.desc())
    ).all()
    chosen = next((p for p in plans if p.status in CHOSEN_STATUSES), None)
    blocked = [p for p in plans if p.status == "blocked"]
    executions, recovery = [], None
    if chosen is not None:
        executions = db.scalars(
            select(MitigationExecution).where(
                MitigationExecution.plan_id == chosen.id, MitigationExecution.organization_id == org,
            ).order_by(MitigationExecution.started_at.desc())
        ).all()
        if executions:
            recovery = _latest_check(db, org, executions[0].id)
    event = db.scalars(
        select(IncidentEvent).where(
            IncidentEvent.organization_id == org, IncidentEvent.incident_id == inc.id,
            IncidentEvent.kind == "no_safe_action",
        ).order_by(IncidentEvent.occurred_at.desc())
    ).first()
    running = db.scalars(
        select(PipelineRun).where(
            PipelineRun.organization_id == org, PipelineRun.incident_id == inc.id,
            PipelineRun.status == "running", PipelineRun.trigger_kind.in_(PIPELINE_KINDS),
        )
    ).first()
    latest_exec = executions[0] if executions else None
    pending = bool(
        running is not None
        or (chosen is not None and chosen.status in PENDING_PLAN)
        or (latest_exec is not None and latest_exec.status == "running")
        or (latest_exec is not None and latest_exec.status == "succeeded" and recovery is None)
    )
    return {
        "chosen": _plan_out(chosen, inc) if chosen else None,
        "blocked": [_plan_out(p, inc) for p in blocked],
        "executions": [_exec_out(e, recovery if i == 0 else _latest_check(db, org, e.id))
                       for i, e in enumerate(executions)],
        "recovery": _check_out(recovery) if recovery else None,
        "no_safe_action": event.message if event else None,
        "pending": pending,
    }


@router.get("/mitigation-plans/{plan_id}/executions")
def list_executions(plan_id: uuid.UUID, org: str = Depends(get_organization_id), db: Session = Depends(get_db)):
    plan = _plan_for_org(db, org, plan_id)
    rows = db.scalars(
        select(MitigationExecution).where(
            MitigationExecution.plan_id == plan.id, MitigationExecution.organization_id == org,
        ).order_by(MitigationExecution.started_at.desc())
    ).all()
    return [_exec_out(e, _latest_check(db, org, e.id)) for e in rows]


@router.post("/mitigation-plans/{plan_id}/approve", status_code=202)
def approve_plan(plan_id: uuid.UUID, body: DecisionBody, org: str = Depends(get_organization_id),
                 actor: str = Depends(get_actor), db: Session = Depends(get_db),
                 settings: Settings = Depends(get_settings)):
    plan = _plan_for_org(db, org, plan_id)
    if plan.status != "proposed":
        raise HTTPException(409, detail={"error": "not_proposed", "status": plan.status})
    incident = db.get(Incident, plan.incident_id)
    if incident is None or incident.organization_id != org:
        raise HTTPException(404, "incident not found")
    gates = recheck_gates(db, incident, plan)
    plan.gate_results = gates
    failed = [g for g in gates if not g.get("ok")]
    if failed:
        plan.status = "blocked"
        plan.rationale = (plan.rationale + " Blocked at approval: "
                          + "; ".join(g["detail"] for g in failed)).strip()
        db.commit()
        raise HTTPException(409, detail={"error": "gates_failed", "gates": failed})
    now = datetime.now(timezone.utc)
    plan.status = "approved"
    plan.decided_by = actor
    plan.decided_at = now
    incident.updated_at = now
    db.add(IncidentEvent(
        organization_id=org, incident_id=incident.id, kind="mitigation_approved",
        message=f"Approved {plan.action_type} by {actor}"
                + (f": {body.comment}" if body.comment else ""),
        data={"plan_id": str(plan.id), "actor": actor, "comment": body.comment},
    ))
    job = maybe_enqueue_execute(db, settings, plan, incident, actor)
    db.commit()
    return {"status": "approved", "queued": True, "job_id": str(job.id)}


@router.post("/mitigation-plans/{plan_id}/reject", status_code=200)
def reject_plan(plan_id: uuid.UUID, body: DecisionBody, org: str = Depends(get_organization_id),
                actor: str = Depends(get_actor), db: Session = Depends(get_db)):
    plan = _plan_for_org(db, org, plan_id)
    if plan.status != "proposed":
        raise HTTPException(409, detail={"error": "not_proposed", "status": plan.status})
    incident = db.get(Incident, plan.incident_id)
    if incident is None or incident.organization_id != org:
        raise HTTPException(404, "incident not found")
    now = datetime.now(timezone.utc)
    plan.status = "rejected"
    plan.decided_by = actor
    plan.decided_at = now
    incident.updated_at = now
    db.add(IncidentEvent(
        organization_id=org, incident_id=incident.id, kind="mitigation_rejected",
        message=f"Rejected {plan.action_type} by {actor}"
                + (f": {body.comment}" if body.comment else ""),
        data={"plan_id": str(plan.id), "actor": actor, "comment": body.comment},
    ))
    db.commit()
    return {"status": "rejected"}
