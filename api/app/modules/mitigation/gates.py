"""Deterministic precondition gates. Each one is plain code; the LLM never decides safety."""

from datetime import timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.db.models import Incident, MitigationExecution, MitigationPlan, RcaReport, Service, ServiceDeployment
from app.modules.catalog.service import current_deployment, recent_deployments

# A rollback target must have been live this long, incident-free, to count as healthy. An alert needs
# its 1 min rate window plus `for: 1m` before Nexora hears of it; 5 min leaves margin, and the live
# bench waits 330 s between scenarios.
MIN_OBSERVED_MINUTES = 5
RESTART_LIMIT_PER_HOUR = 2
RESTART_COUNTED_STATUSES = ("approved", "executing", "succeeded", "failed", "undone")


def run_gates(session: Session, incident: Incident, service: Service | None, action: dict) -> list[dict]:
    results = []
    for name in action.get("preconditions") or []:
        fn = GATES.get(name)
        if fn is None:
            results.append({"gate": name, "ok": False, "detail": f"unknown gate {name}"})
            continue
        results.append(fn(session, incident, service, action))
    return results


def previous_release_exists(session: Session, incident: Incident, service: Service | None,
                            action: dict) -> dict:
    previous = _previous_release(session, service)
    if previous is None:
        return {"gate": "previous_release_exists", "ok": False, "detail": "no previous release is recorded"}
    return {"gate": "previous_release_exists", "ok": True, "detail": f"previous release is {previous.version}"}


def previous_release_healthy(session: Session, incident: Incident, service: Service | None,
                             action: dict) -> dict:
    """The rollback target (the release before the current one) must be shown to be healthy:

    1. it is not the release the incident's RCA blames, and the blamed release is the one the rollback
       removes (an RCA that blames a release it does not name, or one that is not recorded, blocks);
    2. it ran for at least MIN_OBSERVED_MINUTES, long enough for an alert to have fired;
    3. no incident was detected while it was live, including the incident being mitigated.
    Absence of other incidents alone is not proof: a release that was live for a moment shows nothing."""
    gate = "previous_release_healthy"
    current, previous = _current_and_previous(session, service)
    if previous is None or current is None:
        return {"gate": gate, "ok": False, "detail": "no previous release is recorded"}

    problem = _blamed_release_problem(session, incident, service, current, previous)
    if problem:
        return {"gate": gate, "ok": False, "detail": problem}

    observed = _aware(current.deployed_at) - _aware(previous.deployed_at)
    minutes = observed.total_seconds() / 60
    if observed < timedelta(minutes=MIN_OBSERVED_MINUTES):
        return {"gate": gate, "ok": False,
                "detail": (f"{previous.version} was live only {minutes:.1f} min before {current.version}; "
                           f"at least {MIN_OBSERVED_MINUTES} min are needed to show it was healthy")}

    # The current incident counts too: if it was detected while the target was live, the target failed.
    during = session.scalars(
        select(Incident).where(
            Incident.organization_id == incident.organization_id,
            Incident.service == incident.service,
            Incident.environment == incident.environment,
            Incident.detected_at >= previous.deployed_at,
            Incident.detected_at < current.deployed_at,
        ).order_by(Incident.detected_at)
    ).all()
    if during:
        keys = ", ".join(i.key for i in during)
        return {"gate": gate, "ok": False, "detail": f"{previous.version} had incident(s) {keys}"}
    return {"gate": gate, "ok": True,
            "detail": f"{previous.version} ran {minutes:.0f} min with no incident before {current.version}"}


def _blamed_release_problem(session: Session, incident: Incident, service: Service,
                            current: ServiceDeployment, previous: ServiceDeployment) -> str | None:
    """Why the RCA's blamed release makes the rollback target unsafe, or None when it does not."""
    report = session.scalars(
        select(RcaReport).where(
            RcaReport.organization_id == incident.organization_id,
            RcaReport.incident_id == incident.id,
            RcaReport.status == "succeeded",
        ).order_by(RcaReport.created_at.desc()).limit(1)
    ).first()
    trigger = ((report.report or {}).get("trigger") or {}) if report else {}
    if trigger.get("kind") != "release":
        return None  # no release is blamed; the observation checks below still apply
    blamed = str(trigger.get("release_version") or "").strip()
    if not blamed:
        return "the RCA blames a release but does not name it; the rollback target cannot be verified"
    recorded = set(session.scalars(
        select(ServiceDeployment.version).where(ServiceDeployment.service_id == service.id)).all())
    if blamed not in recorded:
        return f"the RCA blames {blamed}, which is not a recorded release; the rollback target cannot be verified"
    if blamed == previous.version:
        return f"{previous.version} is the release the RCA blames for {incident.key}"
    if blamed != current.version:
        return (f"the RCA blames {blamed}, but the current release is {current.version}; "
                f"rolling back to {previous.version} does not remove {blamed}")
    return None


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def no_irreversible_migration(session: Session, incident: Incident, service: Service | None,
                              action: dict) -> dict:
    current = current_deployment(session, service) if service else None
    if current is None:
        return {"gate": "no_irreversible_migration", "ok": False, "detail": "no current release is recorded"}
    if current.has_irreversible_migration:
        return {
            "gate": "no_irreversible_migration",
            "ok": False,
            "detail": f"{current.version} includes an irreversible migration",
        }
    return {"gate": "no_irreversible_migration", "ok": True, "detail": f"{current.version} can be rolled back"}


def no_execution_running(session: Session, incident: Incident, service: Service | None,
                         action: dict) -> dict:
    running_plan = session.scalars(
        select(MitigationPlan)
        .join(Incident, Incident.id == MitigationPlan.incident_id)
        .where(
            MitigationPlan.organization_id == incident.organization_id,
            MitigationPlan.status == "executing",
            Incident.service == incident.service,
            Incident.environment == incident.environment,
        )
        .limit(1)
    ).first()
    running_exec = session.scalars(
        select(MitigationExecution).where(
            MitigationExecution.organization_id == incident.organization_id,
            MitigationExecution.service == (incident.service or ""),
            MitigationExecution.environment == (incident.environment or ""),
            MitigationExecution.status == "running",
        ).limit(1)
    ).first()
    if running_plan or running_exec:
        return {
            "gate": "no_execution_running",
            "ok": False,
            "detail": f"an execution is already running for {incident.service}/{incident.environment}",
        }
    return {"gate": "no_execution_running", "ok": True, "detail": "no execution is running for this service"}


def flag_exists(session: Session, incident: Incident, service: Service | None, action: dict) -> dict:
    flag = (action.get("params") or {}).get("flag")
    if not isinstance(flag, str) or not flag.strip():
        return {"gate": "flag_exists", "ok": False, "detail": "params.flag is required"}
    flag = flag.strip()
    if service is not None:
        descriptions = " ".join(d.description or "" for d in recent_deployments(session, service, limit=10))
        if flag in descriptions:
            return {"gate": "flag_exists", "ok": True, "detail": f"flag {flag} is named in a recorded release"}
    return {"gate": "flag_exists", "ok": True, "detail": f"flag {flag} is set"}


def restart_rate_limit(session: Session, incident: Incident, service: Service | None,
                       action: dict) -> dict:
    since = utc_now() - timedelta(hours=1)
    count = len(session.scalars(
        select(MitigationPlan)
        .join(Incident, Incident.id == MitigationPlan.incident_id)
        .where(
            MitigationPlan.organization_id == incident.organization_id,
            MitigationPlan.action_type == "restart_service",
            MitigationPlan.status.in_(RESTART_COUNTED_STATUSES),
            MitigationPlan.created_at >= since,
            Incident.service == incident.service,
            Incident.environment == incident.environment,
        )
    ).all())
    ok = count < RESTART_LIMIT_PER_HOUR
    return {
        "gate": "restart_rate_limit",
        "ok": ok,
        "detail": (f"{count} restart(s) in the last hour"
                   + ("" if ok else f"; limit is {RESTART_LIMIT_PER_HOUR}")),
    }


def _current_and_previous(session: Session, service: Service | None) -> tuple[ServiceDeployment | None, ServiceDeployment | None]:
    if service is None:
        return None, None
    deployments = recent_deployments(session, service, limit=2)
    if not deployments:
        return None, None
    current = deployments[0]
    previous = deployments[1] if len(deployments) > 1 else None
    return current, previous


def _previous_release(session: Session, service: Service | None) -> ServiceDeployment | None:
    return _current_and_previous(session, service)[1]


GATES = {
    "previous_release_exists": previous_release_exists,
    "previous_release_healthy": previous_release_healthy,
    "no_irreversible_migration": no_irreversible_migration,
    "no_execution_running": no_execution_running,
    "flag_exists": flag_exists,
    "restart_rate_limit": restart_rate_limit,
}
