from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import Settings, get_settings
from app.core.identity import get_organization_id
from app.db.base import get_db
from app.db.models import Incident, PipelineRun
from app.modules.alert_ingestion.gateway import UNAUTHENTICATED_ORG

router = APIRouter(prefix="/api/v1/pipeline", tags=["pipeline"])


@router.get("/runs")
def list_runs(
    org: str = Depends(get_organization_id),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    limit: int = 30,
):
    orgs = [org]
    if settings.is_development:
        orgs.append(UNAUTHENTICATED_ORG)  # show rejected webhooks while developing
    runs = db.scalars(
        select(PipelineRun).where(PipelineRun.organization_id.in_(orgs))
        .options(selectinload(PipelineRun.steps))
        .order_by(PipelineRun.started_at.desc()).limit(min(limit, 100))
    ).all()
    keys = {}
    ids = [r.incident_id for r in runs if r.incident_id]
    if ids:
        keys = {i.id: i.key for i in db.scalars(select(Incident).where(Incident.id.in_(ids))).all()}
    return [
        {
            "id": str(r.id), "title": r.title, "trigger_kind": r.trigger_kind, "status": r.status,
            "started_at": r.started_at, "finished_at": r.finished_at,
            "incident_id": str(r.incident_id) if r.incident_id else None,
            "incident_key": keys.get(r.incident_id),
            "steps": [
                {"seq": s.seq, "stage": s.stage, "name": s.name, "status": s.status, "detail": s.detail,
                 "duration_ms": s.duration_ms, "finished_at": s.finished_at}
                for s in r.steps
            ],
        }
        for r in runs
    ]
