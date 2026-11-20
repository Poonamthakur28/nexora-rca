import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.identity import get_actor, get_organization_id
from app.db.base import get_db
from app.db.models import FixAttempt, Incident

from .engine import PENDING, fix_settings, maybe_enqueue_fix

router = APIRouter(prefix="/api/v1", tags=["fix"])


def _out(a: FixAttempt) -> dict:
    return {
        "id": str(a.id), "incident_id": str(a.incident_id),
        "rca_report_id": str(a.rca_report_id) if a.rca_report_id else None,
        "status": a.status, "reason": a.reason, "provider": a.provider, "model": a.model,
        "target_file": a.target_file, "repro_test_path": a.repro_test_path, "repro_test_code": a.repro_test_code,
        "patch_diff": a.patch_diff, "patch_files": a.patch_files, "explanation": a.explanation,
        "gates": a.gates, "runs": a.runs, "artifacts_dir": a.artifacts_dir, "pr_url": a.pr_url,
        "requested_by": a.requested_by, "llm_calls": a.llm_calls, "input_tokens": a.input_tokens,
        "output_tokens": a.output_tokens, "created_at": a.created_at, "finished_at": a.finished_at,
    }


def _incident(db: Session, org: str, incident_id: uuid.UUID) -> Incident:
    inc = db.scalars(select(Incident).where(Incident.id == incident_id, Incident.organization_id == org)).first()
    if inc is None:
        raise HTTPException(404, "incident not found")
    return inc


@router.get("/fix/config")
def fix_config(settings: Settings = Depends(get_settings)) -> dict:
    """How fixes are produced (never returns keys or tokens)."""
    f = fix_settings(settings)
    key_set = {"anthropic": bool(settings.anthropic_api_key), "openai": bool(settings.openai_api_key)}
    return {"enabled": settings.fix_enabled, "provider": f.rca_provider,
            "configured": f.rca_provider != "heuristic" and key_set.get(f.rca_provider, True),
            "sandbox": settings.fix_sandbox, "draft_pr": bool(settings.github_token)}


@router.get("/incidents/{incident_id}/fix-attempts")
def list_fix_attempts(incident_id: uuid.UUID, org: str = Depends(get_organization_id),
                      db: Session = Depends(get_db)) -> dict:
    inc = _incident(db, org, incident_id)
    rows = db.scalars(select(FixAttempt).where(FixAttempt.organization_id == org, FixAttempt.incident_id == inc.id)
                      .order_by(FixAttempt.created_at.desc()).limit(10)).all()
    return {"latest": _out(rows[0]) if rows else None, "pending": any(r.status in PENDING for r in rows),
            "history": [{"id": str(r.id), "status": r.status, "reason": r.reason, "created_at": r.created_at}
                        for r in rows]}


@router.post("/incidents/{incident_id}/fix-attempts", status_code=202)
def request_fix(incident_id: uuid.UUID, org: str = Depends(get_organization_id), actor: str = Depends(get_actor),
                db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    inc = _incident(db, org, incident_id)
    attempt, reason = maybe_enqueue_fix(db, settings, inc, requested_by=actor)
    if attempt is None:
        raise HTTPException(409, reason or "a fix cannot be attempted")
    db.commit()
    return {"queued": True, "attempt_id": str(attempt.id)}


@router.get("/fix-attempts/{attempt_id}/patch", response_class=PlainTextResponse)
def get_patch(attempt_id: uuid.UUID, org: str = Depends(get_organization_id), db: Session = Depends(get_db)) -> str:
    a = db.scalars(select(FixAttempt).where(FixAttempt.id == attempt_id, FixAttempt.organization_id == org)).first()
    if a is None:
        raise HTTPException(404, "fix attempt not found")
    if not a.patch_diff:
        raise HTTPException(404, "this attempt produced no verified patch")
    return a.patch_diff
