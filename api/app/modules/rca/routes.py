import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.identity import get_organization_id
from app.db.base import get_db
from app.db.models import Incident, PipelineRun, RcaReport

from .engine import maybe_enqueue_investigation
from .providers import DEFAULT_MODELS

router = APIRouter(prefix="/api/v1", tags=["rca"])


def _out(r: RcaReport) -> dict:
    return {"id": str(r.id), "status": r.status, "provider": r.provider, "model": r.model,
            "category": r.category, "confidence": r.confidence, "grounded": r.grounded, "report": r.report,
            "checks": r.checks, "error": r.error, "latency_ms": r.latency_ms, "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens, "collection_id": str(r.collection_id) if r.collection_id else None,
            "created_at": r.created_at}


@router.get("/rca/config")
def rca_config(settings: Settings = Depends(get_settings)) -> dict:
    """Which provider investigates (never returns keys)."""
    p = settings.rca_provider
    key_set = {"anthropic": bool(settings.anthropic_api_key), "openai": bool(settings.openai_api_key)}.get(p, True)
    return {"enabled": settings.rca_enabled, "provider": p,
            "model": settings.rca_model or DEFAULT_MODELS.get(p, "rules-v1"), "configured": key_set}


@router.get("/incidents/{incident_id}/rca")
def get_rca(incident_id: uuid.UUID, org: str = Depends(get_organization_id), db: Session = Depends(get_db)):
    inc = db.scalars(select(Incident).where(Incident.id == incident_id, Incident.organization_id == org)).first()
    if inc is None:
        raise HTTPException(404, "incident not found")
    rows = db.scalars(select(RcaReport).where(RcaReport.incident_id == inc.id, RcaReport.organization_id == org)
                      .order_by(RcaReport.created_at.desc()).limit(10)).all()
    running = db.scalars(select(PipelineRun).where(PipelineRun.organization_id == org,
                                                   PipelineRun.incident_id == inc.id,
                                                   PipelineRun.trigger_kind == "rca_investigation",
                                                   PipelineRun.status == "running")).first()
    return {"latest": _out(rows[0]) if rows else None, "in_progress": running is not None,
            "history": [{"id": str(r.id), "status": r.status, "provider": r.provider, "model": r.model,
                         "category": r.category, "confidence": r.confidence, "created_at": r.created_at}
                        for r in rows]}


@router.post("/incidents/{incident_id}/rca/investigate", status_code=202)
def investigate_again(incident_id: uuid.UUID, org: str = Depends(get_organization_id),
                      db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    inc = db.scalars(select(Incident).where(Incident.id == incident_id, Incident.organization_id == org)).first()
    if inc is None:
        raise HTTPException(404, "incident not found")
    if not settings.rca_enabled:
        raise HTTPException(409, "investigation is disabled (RCA_ENABLED=false)")
    job = maybe_enqueue_investigation(db, settings, inc, "requested from the UI")
    db.commit()
    return {"queued": job is not None}
