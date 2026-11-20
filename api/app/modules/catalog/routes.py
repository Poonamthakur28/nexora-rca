from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.identity import get_organization_id
from app.db.base import get_db
from app.db.models import Service

from .service import record_deployment, recent_deployments

router = APIRouter(prefix="/api/v1/services", tags=["service-catalog"])


def _dep(d) -> dict:
    return {"id": str(d.id), "version": d.version, "commit_sha": d.commit_sha, "description": d.description,
            "changed_files": d.changed_files, "source": d.source, "deployed_at": d.deployed_at,
            "has_irreversible_migration": d.has_irreversible_migration}


def _svc(s: Service, db: Session) -> dict:
    return {"id": str(s.id), "name": s.name, "environment": s.environment, "owners": s.owners,
            "repository_url": s.repository_url, "source_root": s.source_root,
            "telemetry_service_name": s.telemetry_service_name, "dependencies": s.dependencies,
            "deployments": [_dep(d) for d in recent_deployments(db, s)]}


@router.get("")
def list_services(org: str = Depends(get_organization_id), db: Session = Depends(get_db)):
    return [_svc(s, db) for s in db.scalars(select(Service).where(Service.organization_id == org)).all()]


class DeploymentIn(BaseModel):
    environment: str = "local"
    version: str = Field(min_length=1, max_length=50)
    commit_sha: str | None = None
    description: str = ""
    changed_files: list[str] = Field(default_factory=list, max_length=500)
    has_irreversible_migration: bool = False


@router.post("/{name}/deployments", status_code=201)
def add_deployment(name: str, body: DeploymentIn, org: str = Depends(get_organization_id),
                   db: Session = Depends(get_db)):
    """Deploy marker, called by CI after a release goes live."""
    svc = db.scalars(select(Service).where(Service.organization_id == org, Service.name == name,
                                           Service.environment == body.environment)).first()
    if svc is None:
        raise HTTPException(404, "service not found in catalog")
    dep = record_deployment(db, svc, body.version, body.description, body.changed_files, body.commit_sha,
                            source="ci", has_irreversible_migration=body.has_irreversible_migration)
    return _dep(dep)
