import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.identity import get_organization_id
from app.db.base import get_db
from app.db.models import Alert, AlertIncidentLink, Incident, IncidentEvent

router = APIRouter(prefix="/api/v1/incidents", tags=["incidents"])


def _out(i: Incident) -> dict:
    return {
        "id": str(i.id), "key": i.key, "title": i.title, "service": i.service, "environment": i.environment,
        "severity": i.severity, "status": i.status, "active_alerts": i.active_alerts,
        "detected_at": i.detected_at, "updated_at": i.updated_at,
    }


@router.get("")
def list_incidents(org: str = Depends(get_organization_id), db: Session = Depends(get_db), limit: int = 50):
    rows = db.scalars(
        select(Incident).where(Incident.organization_id == org)
        .order_by(Incident.detected_at.desc()).limit(min(limit, 200))
    ).all()
    return [_out(i) for i in rows]


@router.get("/{incident_id}")
def get_incident(incident_id: uuid.UUID, org: str = Depends(get_organization_id), db: Session = Depends(get_db)):
    inc = db.scalars(select(Incident).where(Incident.id == incident_id, Incident.organization_id == org)).first()
    if inc is None:
        raise HTTPException(404, "incident not found")
    rows = db.execute(
        select(Alert, AlertIncidentLink.ended_at).join(AlertIncidentLink, AlertIncidentLink.alert_id == Alert.id)
        .where(AlertIncidentLink.incident_id == inc.id, Alert.organization_id == org)
    ).all()
    events = db.scalars(
        select(IncidentEvent).where(IncidentEvent.incident_id == inc.id, IncidentEvent.organization_id == org)
        .order_by(IncidentEvent.occurred_at)
    ).all()
    return {
        **_out(inc),
        # An alert that later recurred into another incident shows as "moved" here.
        "alerts": [{"id": str(a.id), "title": a.title, "status": "moved" if ended else a.normalized_status,
                    "severity": a.normalized_severity, "started_at": a.started_at,
                    "resolved_at": a.resolved_at, "moved_at": ended} for a, ended in rows],
        "timeline": [{"at": e.occurred_at, "kind": e.kind, "message": e.message} for e in events],
    }
