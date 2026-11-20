import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.identity import get_organization_id
from app.db.base import get_db
from app.db.models import Alert, AlertDelivery, AlertIncidentLink, Incident

from .gateway import receive_alertmanager

router = APIRouter(prefix="/api/v1", tags=["alerts"])


@router.post("/integrations/alerts/alertmanager", status_code=202)
async def alertmanager_webhook(
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    body = await request.body()
    result = await run_in_threadpool(receive_alertmanager, db, settings, authorization, body)
    return JSONResponse(result.body, status_code=result.status_code)


def _alert_out(a: Alert) -> dict:
    return {
        "id": str(a.id), "provider": a.provider, "external_alert_id": a.external_alert_id,
        "fingerprint": a.fingerprint, "title": a.title, "description": a.description,
        "service": a.service, "environment": a.environment,
        "severity": a.normalized_severity, "provider_severity": a.provider_severity,
        "status": a.normalized_status, "provider_status": a.provider_status,
        "started_at": a.started_at, "resolved_at": a.resolved_at, "last_seen_at": a.last_seen_at,
        "version": a.version, "alertname": (a.attributes or {}).get("alertname"),
    }


@router.get("/alerts")
def list_alerts(org: str = Depends(get_organization_id), db: Session = Depends(get_db), limit: int = 50):
    rows = db.scalars(
        select(Alert).where(Alert.organization_id == org).order_by(Alert.last_seen_at.desc()).limit(min(limit, 200))
    ).all()
    return [_alert_out(a) for a in rows]


@router.get("/alerts/{alert_id}")
def get_alert(alert_id: uuid.UUID, org: str = Depends(get_organization_id), db: Session = Depends(get_db)):
    alert = db.scalars(select(Alert).where(Alert.id == alert_id, Alert.organization_id == org)).first()
    if alert is None:
        raise HTTPException(404, "alert not found")
    incidents = db.scalars(
        select(Incident).join(AlertIncidentLink, AlertIncidentLink.incident_id == Incident.id)
        .where(AlertIncidentLink.alert_id == alert.id, Incident.organization_id == org)
    ).all()
    return {**_alert_out(alert), "attributes": alert.attributes,
            "incidents": [{"id": str(i.id), "key": i.key, "status": i.status} for i in incidents]}


@router.get("/alerts/{alert_id}/deliveries")
def alert_deliveries(alert_id: uuid.UUID, org: str = Depends(get_organization_id), db: Session = Depends(get_db)):
    alert = db.scalars(select(Alert).where(Alert.id == alert_id, Alert.organization_id == org)).first()
    if alert is None:
        raise HTTPException(404, "alert not found")
    deliveries = db.scalars(
        select(AlertDelivery).where(AlertDelivery.organization_id == org,
                                    AlertDelivery.integration_id == alert.integration_id)
        .order_by(AlertDelivery.received_at.desc()).limit(200)
    ).all()
    matching = [
        d for d in deliveries
        if any(x.get("fingerprint") == alert.external_alert_id for x in (d.raw_payload or {}).get("alerts", []))
    ]
    return [{"id": str(d.id), "received_at": d.received_at, "processing_status": d.processing_status,
             "processing_error": d.processing_error, "alert_count": d.alert_count} for d in matching]
