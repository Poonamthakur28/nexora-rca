"""Service catalog: which code runs for a service, who owns it, and what was deployed when."""

import logging
from datetime import datetime
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import utc_now
from app.db.models import Service, ServiceDeployment

log = logging.getLogger("nexora.catalog")


def catalog_path(settings: Settings) -> Path:
    if settings.service_catalog_file:
        return Path(settings.service_catalog_file)
    return Path(__file__).resolve().parents[4] / "deploy" / "config" / "service-catalog.yaml"


OVERRIDABLE_FIELDS = ("source_root", "runtime_path_prefix")


def load_overrides(settings: Settings) -> dict[tuple[str, str], dict]:
    """Machine-local paths per (service, environment) from SERVICE_CATALOG_OVERRIDES_FILE.

    Only where the code lives can be overridden; owners, metrics and actions always come from the shared
    catalog. An unset, missing or unreadable file means no overrides (logged), never a failed seed."""
    if not settings.service_catalog_overrides_file:
        return {}
    path = Path(settings.service_catalog_overrides_file)
    try:
        data = yaml.safe_load(path.read_text()) or {}
        entries = data.get("services") or []
    except Exception as exc:
        log.warning("ignoring service catalog overrides %s: %s", path, exc)
        return {}
    out: dict[tuple[str, str], dict] = {}
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name"):
            out[(entry["name"], entry.get("environment", "local"))] = {
                k: entry[k] for k in OVERRIDABLE_FIELDS if k in entry}
    return out


def seed_from_file(session: Session, settings: Settings) -> list[Service]:
    """Upsert services from the catalog YAML. Adds a baseline deployment for new services."""
    path = catalog_path(settings)
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    overrides = load_overrides(settings)
    org = settings.default_organization_id
    services = []
    for entry in data.get("services", []):
        svc = find_service(session, org, entry["name"], entry.get("environment", "local"))
        fields = dict(
            owners=entry.get("owners", []), repository_url=entry.get("repository_url"),
            source_root=entry.get("source_root"), runtime_path_prefix=entry.get("runtime_path_prefix"),
            telemetry_service_name=entry.get("telemetry_service_name", entry["name"]),
            dependencies=entry.get("dependencies", []),
            config={k: entry[k] for k in ("language", "app_namespaces", "metrics", "actions") if k in entry},
        )
        fields.update(overrides.get((entry["name"], entry.get("environment", "local")), {}))
        if svc is None:
            svc = Service(organization_id=org, name=entry["name"], environment=entry.get("environment", "local"),
                          **fields)
            session.add(svc)
            session.flush()
            base = entry.get("baseline_deployment")
            if base:
                session.add(ServiceDeployment(
                    organization_id=org, service_id=svc.id, version=base["version"],
                    commit_sha=base.get("commit_sha"), description=base.get("description", ""),
                    changed_files=[], source="catalog_seed",
                ))
        else:
            for k, v in fields.items():
                setattr(svc, k, v)
            svc.updated_at = utc_now()
        services.append(svc)
    session.commit()
    return services


def find_service(session: Session, org: str, name: str | None, environment: str | None) -> Service | None:
    if not name:
        return None
    stmt = select(Service).where(Service.organization_id == org, Service.name == name)
    if environment:
        stmt = stmt.where(Service.environment == environment)
    return session.scalars(stmt).first()


def record_deployment(session: Session, service: Service, version: str, description: str,
                      changed_files: list[str], commit_sha: str | None = None, source: str = "manual",
                      has_irreversible_migration: bool = False) -> ServiceDeployment:
    dep = ServiceDeployment(
        organization_id=service.organization_id, service_id=service.id, version=version, commit_sha=commit_sha,
        description=description, changed_files=changed_files, source=source,
        has_irreversible_migration=has_irreversible_migration,
    )
    session.add(dep)
    session.commit()
    return dep


def current_deployment(session: Session, service: Service, at: datetime | None = None) -> ServiceDeployment | None:
    stmt = select(ServiceDeployment).where(ServiceDeployment.service_id == service.id)
    if at is not None:
        stmt = stmt.where(ServiceDeployment.deployed_at <= at)
    return session.scalars(stmt.order_by(ServiceDeployment.deployed_at.desc()).limit(1)).first()


def recent_deployments(session: Session, service: Service, limit: int = 5) -> list[ServiceDeployment]:
    return list(session.scalars(
        select(ServiceDeployment).where(ServiceDeployment.service_id == service.id)
        .order_by(ServiceDeployment.deployed_at.desc()).limit(limit)
    ).all())


def next_version(session: Session, service: Service) -> str:
    """v1.2.3 -> v1.3.0 for simulated test-bench releases."""
    cur = current_deployment(session, service)
    try:
        major, minor, _ = (cur.version.lstrip("v").split(".") if cur else ["1", "0", "0"])
        return f"v{int(major)}.{int(minor) + 1}.0"
    except ValueError:
        return "v1.1.0"


BENCH_RELEASE_NOTES = {
    "code_null_tax": ("Refactor tax rule lookup in find_tax_rule", "Restore default tax rule for unknown regions"),
    "code_inventory_keyerror": ("Simplify stock lookup in available_stock", "Treat unknown SKUs as out of stock"),
    "db_pool_leak": ("Streamline order writes in save_order", "Always return database connections in save_order"),
}


def record_bench_release(session: Session, settings: Settings, bug_enabled: bool, bug: str = "code_null_tax",
                         files: list[str] | None = None) -> str | None:
    """Test bench only: turning a planted code/resource bug on or off is recorded as a release of
    shop-demo, so evidence collection sees a deploy that changed the faulty file right before the errors."""
    svc = find_service(session, settings.default_organization_id, settings.monitored_service, "local")
    if svc is None:
        return None
    version = next_version(session, svc)
    intro, fix = BENCH_RELEASE_NOTES.get(bug, (f"Change related to {bug}", f"Revert change related to {bug}"))
    note = "introduces" if bug_enabled else "removes"
    desc = f"{intro if bug_enabled else fix} (test bench: {note} planted bug {bug})."
    record_deployment(session, svc, version, desc, files or ["app/pricing.py"], commit_sha=f"bench-{version}",
                      source="test_bench")
    return version
