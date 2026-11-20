"""Choose one safe catalog action from RCA mitigation options. The LLM only proposes; this decides."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import Incident, IncidentEvent, Job, MitigationPlan, RcaReport, Service
from app.jobs.queue import enqueue
from app.modules.catalog.service import find_service
from app.modules.pipeline.recorder import PipelineRecorder

from .gates import run_gates

JOB_TYPE = "mitigation.plan"
OPTION_TO_ACTION = {
    "rollback": "rollback_release",
    "disable_feature": "disable_feature",
    "restart": "restart_service",
}
RISK_WEIGHT = {"low": 1.0, "medium": 0.6, "high": 0.3}
CODE_ACTIONS = {"rollback_release", "disable_feature"}
_FLAG_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")


def maybe_enqueue_plan(session: Session, settings: Settings, incident: Incident) -> Job | None:
    """Queue planning unless one is already waiting for this incident."""
    if not settings.mitigation_enabled:
        return None
    pending = session.scalars(select(Job).where(Job.type == JOB_TYPE, Job.status == "queued")).all()
    if any(j.payload.get("incident_id") == str(incident.id) for j in pending):
        return None
    rec = PipelineRecorder.start(session, incident.organization_id, "mitigation_plan",
                                 f"Mitigation plan for {incident.key}", trigger_id=str(incident.id))
    rec.run.incident_id = incident.id
    rec.step("queued", "Plan queued", "success", f"after RCA for {incident.key}")
    return enqueue(session, incident.organization_id, JOB_TYPE,
                   {"incident_id": str(incident.id), "run_id": str(rec.run.id)})


def plan_for(session: Session, settings: Settings, incident: Incident) -> MitigationPlan | None:
    """Evaluate catalog actions for the latest RCA report and persist every candidate."""
    report = _latest_report(session, incident)
    if report is None:
        return None
    service = find_service(session, incident.organization_id, incident.service, incident.environment)
    catalog = _catalog_actions(service)
    options = list((report.report or {}).get("mitigation_options") or [])
    dropped, candidates = [], []
    seen_types: set[str] = set()
    for option in options:
        action_type = OPTION_TO_ACTION.get(option.get("action"))
        catalog_action = catalog.get(action_type) if action_type else None
        if action_type is None or catalog_action is None:
            dropped.append(f"{option.get('action')}: not in the catalog")
            continue
        if action_type in seen_types:
            continue
        seen_types.add(action_type)
        candidates.append(_evaluate(session, incident, service, report, catalog_action, option))

    dropped_note = "; ".join(dropped)
    passing = [p for p in candidates if p.status == "proposed" and p.score > 0]
    chosen = max(passing, key=lambda p: p.score) if passing else None
    for plan in candidates:
        if chosen is None or plan.id == chosen.id:
            if dropped_note:
                plan.rationale = f"{plan.rationale} {dropped_note}".strip()
            continue
        if plan.status == "proposed":
            plan.status = "blocked"
            plan.rationale = (f"not chosen; {chosen.action_type} scored {chosen.score:.2f}. "
                              + plan.rationale).strip()
        elif dropped_note:
            plan.rationale = f"{plan.rationale} {dropped_note}".strip()

    if chosen is None:
        _event(session, incident, "no_safe_action",
               "no safe action; escalate to on-call"
               + (f" ({dropped_note})" if dropped_note else ""))
    else:
        _event(session, incident, "mitigation_proposed",
               f"Proposed {chosen.action_type} (score {chosen.score:.2f}, risk {chosen.risk})")
    session.flush()
    return chosen


def run_plan(session: Session, settings: Settings, incident_id: str, run_id: str | None = None) -> dict:
    incident = session.get(Incident, uuid.UUID(incident_id))
    if incident is None:
        raise LookupError(f"incident {incident_id} not found")
    rec = PipelineRecorder.resume(session, uuid.UUID(run_id)) if run_id else None
    if rec is None:
        rec = PipelineRecorder.start(session, incident.organization_id, "mitigation_plan",
                                     f"Mitigation plan for {incident.key}", trigger_id=str(incident.id))
    rec.run.incident_id = incident.id

    report = _latest_report(session, incident)
    if report is None:
        rec.step("options", "RCA options", "failed", "no succeeded RCA report")
        rec.finish("failed")
        session.commit()
        return {"status": "failed", "error": "no succeeded RCA report"}

    options = list((report.report or {}).get("mitigation_options") or [])
    service = find_service(session, incident.organization_id, incident.service, incident.environment)
    catalog = _catalog_actions(service)
    mapped = [o.get("action") for o in options if OPTION_TO_ACTION.get(o.get("action")) in catalog]
    dropped = [o.get("action") for o in options if OPTION_TO_ACTION.get(o.get("action")) not in catalog]
    rec.step("options", "RCA options mapped", "warning" if dropped and not mapped else "success",
             f"{len(mapped)} catalog action(s)"
             + (f"; dropped {', '.join(f'{a}: not in the catalog' for a in dropped)}" if dropped else ""))

    chosen = plan_for(session, settings, incident)
    plans = session.scalars(
        select(MitigationPlan).where(MitigationPlan.incident_id == incident.id)
        .order_by(MitigationPlan.created_at.desc())
    ).all()
    this_round = [p for p in plans if p.rca_report_id == report.id]
    failed_gates = [g for p in this_round for g in (p.gate_results or []) if not g.get("ok")]
    rec.step("gates", "Preconditions checked", "warning" if failed_gates else "success",
             "; ".join(f"{g['gate']}: {g['detail']}" for g in failed_gates) if failed_gates
             else f"{sum(len(p.gate_results or []) for p in this_round)} gate(s) passed")
    rec.step("choose", "Action chosen", "success" if chosen else "warning",
             f"{chosen.action_type} score {chosen.score:.2f}" if chosen else "no safe action; escalate to on-call")
    rec.step("stored", "Plans stored", "success",
             f"{len(this_round)} plan(s) stored" + (f"; proposed {chosen.action_type}" if chosen else ""))
    rec.finish()
    session.commit()
    return {"status": "succeeded", "chosen": chosen.action_type if chosen else None,
            "plans": len(this_round)}


def record_plan_failure(session: Session, run_id: str | None, error: str, final: bool) -> None:
    if not run_id:
        return
    rec = PipelineRecorder.resume(session, uuid.UUID(run_id))
    if rec:
        rec.step("processing", "Planning failed" + ("" if final else ", will retry"), "failed", error[:300])
        if final:
            rec.finish("failed")
    session.commit()


def _evaluate(session: Session, incident: Incident, service: Service | None, report: RcaReport,
              catalog_action: dict, option: dict) -> MitigationPlan:
    action_type = catalog_action["type"]
    params = _params_for(catalog_action, option)
    gates = run_gates(session, incident, service, {**catalog_action, "params": params})
    risk = catalog_action.get("risk") or option.get("risk") or "medium"
    score = _score(report, action_type, risk)
    gates_ok = all(g.get("ok") for g in gates)
    blocked_reason = [g["detail"] for g in gates if not g.get("ok")]
    if score == 0 and report.category == "dependency" and action_type in CODE_ACTIONS:
        blocked_reason.append("rolling back our code does not fix a dependency failure")
    status = "proposed" if gates_ok and score > 0 else "blocked"
    plan = MitigationPlan(
        organization_id=incident.organization_id,
        incident_id=incident.id,
        rca_report_id=report.id,
        action_type=action_type,
        params=params,
        title=catalog_action.get("title") or action_type,
        risk=risk,
        approval_mode=catalog_action.get("approval") or "single",
        gate_results=gates,
        score=score,
        status=status,
        rationale=_rationale(option, status, score, blocked_reason),
        undo_plan=_undo_plan(action_type, params),
    )
    session.add(plan)
    session.flush()
    return plan


def _score(report: RcaReport, action_type: str, risk: str) -> float:
    confidence = float(report.confidence or 0)
    weight = RISK_WEIGHT.get(risk, 0.3)
    if report.category == "dependency" and action_type in CODE_ACTIONS:
        return 0.0
    return round(confidence * weight, 3)


def _params_for(catalog_action: dict, option: dict) -> dict:
    params = {k: v for k, v in (catalog_action.get("params") or {}).items() if v is not None}
    if catalog_action.get("type") == "disable_feature" and not params.get("flag"):
        flag = _flag_from_target(option.get("target") or "")
        if flag:
            params["flag"] = flag
    return params


def _flag_from_target(target: str) -> str | None:
    tokens = _FLAG_RE.findall(target)
    for token in reversed(tokens):
        if token.startswith(("code_", "slow_", "db_")) or "flag" in token.lower():
            return token
    return tokens[-1] if len(tokens) == 1 else None


def _undo_plan(action_type: str, params: dict) -> dict:
    if action_type == "rollback_release":
        return {"action": "roll_forward", "description": "Re-enable the planted bug and record a roll-forward."}
    if action_type == "disable_feature":
        return {"action": "enable_feature", "description": "Restore the planted failure flag.",
                "params": {"flag": params.get("flag")}}
    if action_type == "restart_service":
        return {"action": "none", "description": "Restart is not reversible."}
    return {"action": "none", "description": "No undo is defined for this action."}


def _rationale(option: dict, status: str, score: float, blocked_reason: list[str]) -> str:
    reason = option.get("reason") or ""
    if status == "proposed":
        return f"{reason} Score {score:.2f}.".strip()
    detail = "; ".join(blocked_reason) or "preconditions failed"
    return f"{reason} Blocked: {detail}.".strip()


def catalog_action_for(service: Service | None, action_type: str) -> dict | None:
    return _catalog_actions(service).get(action_type)


def recheck_gates(session: Session, incident: Incident, plan: MitigationPlan) -> list[dict]:
    """Run the catalog preconditions again; state may have changed since the plan was proposed."""
    service = find_service(session, incident.organization_id, incident.service, incident.environment)
    action = catalog_action_for(service, plan.action_type)
    if action is None:
        return [{"gate": "catalog", "ok": False, "detail": f"{plan.action_type} is not in the catalog"}]
    return run_gates(session, incident, service, {**action, "params": plan.params or {}})


def _catalog_actions(service: Service | None) -> dict[str, dict]:
    if service is None:
        return {}
    actions = (service.config or {}).get("actions") or []
    return {a["type"]: a for a in actions if isinstance(a, dict) and a.get("type")}


def _latest_report(session: Session, incident: Incident) -> RcaReport | None:
    return session.scalars(
        select(RcaReport).where(
            RcaReport.organization_id == incident.organization_id,
            RcaReport.incident_id == incident.id,
            RcaReport.status == "succeeded",
        ).order_by(RcaReport.created_at.desc()).limit(1)
    ).first()


def _event(session: Session, incident: Incident, kind: str, message: str) -> None:
    incident.updated_at = datetime.now(timezone.utc)
    session.add(IncidentEvent(organization_id=incident.organization_id, incident_id=incident.id,
                              kind=kind, message=message[:1000]))
