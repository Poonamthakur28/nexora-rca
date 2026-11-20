import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.identity import get_organization_id
from app.db.base import get_db
from app.db.models import Evidence, Incident, PipelineRun
from app.jobs.queue import enqueue

from .collect import JOB_TYPE, start_collection_run

router = APIRouter(prefix="/api/v1/incidents", tags=["evidence"])

KIND_ORDER = {"code": 0, "trace": 1, "log": 2, "dependency": 3, "deployment": 4, "metric": 5, "gap": 9}


def _incident(db: Session, org: str, incident_id: uuid.UUID) -> Incident:
    inc = db.scalars(select(Incident).where(Incident.id == incident_id, Incident.organization_id == org)).first()
    if inc is None:
        raise HTTPException(404, "incident not found")
    return inc


@router.get("/{incident_id}/evidence")
def get_evidence(incident_id: uuid.UUID, org: str = Depends(get_organization_id), db: Session = Depends(get_db)):
    """Evidence from the latest collection, most relevant first."""
    inc = _incident(db, org, incident_id)
    latest = db.scalars(
        select(PipelineRun).where(PipelineRun.organization_id == org, PipelineRun.incident_id == inc.id,
                                  PipelineRun.trigger_kind == "evidence_collection")
        .order_by(PipelineRun.started_at.desc()).limit(1)
    ).first()
    if latest is None:
        return {"collection": None, "items": []}
    items = db.scalars(select(Evidence).where(Evidence.organization_id == org, Evidence.collection_id == latest.id)).all()
    items = sorted(items, key=lambda e: (KIND_ORDER.get(e.kind, 5), -e.relevance))
    return {
        "collection": {"id": str(latest.id), "status": latest.status, "started_at": latest.started_at,
                       "finished_at": latest.finished_at},
        "items": [{"id": str(e.id), "kind": e.kind, "source": e.source, "title": e.title, "summary": e.summary,
                   "content": e.content, "relevance": e.relevance, "observed_at": e.observed_at} for e in items],
    }


@router.post("/{incident_id}/evidence/collect", status_code=202)
def recollect(incident_id: uuid.UUID, org: str = Depends(get_organization_id), db: Session = Depends(get_db)):
    inc = _incident(db, org, incident_id)
    rec = start_collection_run(db, inc)
    rec.run.incident_id = inc.id
    rec.step("queued", "Collection job queued", "success", "requested from the UI")
    job = enqueue(db, org, JOB_TYPE, {"incident_id": str(inc.id), "run_id": str(rec.run.id)}, max_attempts=3)
    db.commit()
    return {"status": "queued", "job_id": str(job.id), "run_id": str(rec.run.id)}
