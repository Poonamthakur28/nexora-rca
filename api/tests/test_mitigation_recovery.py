import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from app.db.base import utc_now
from app.db.models import (
    Evidence,
    Incident,
    IncidentEvent,
    Job,
    MitigationExecution,
    MitigationPlan,
    PipelineRun,
    RecoveryCheck,
)
from app.modules.mitigation.recovery import maybe_enqueue_verify, run_verify
from tests.test_mitigation_execute import make_plan


def prom_value(value: float):
    def handler(request: httpx.Request) -> httpx.Response:
        if "prom" in request.url.host:
            t = datetime.now(timezone.utc).timestamp()
            return httpx.Response(200, json={"data": {"result": [{"values": [[t, str(value)]]}]}})
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def prom_down():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


def setup_succeeded(session, key="INC-3001", alerts=0) -> tuple[Incident, MitigationPlan, MitigationExecution]:
    plan = make_plan(session, key=key)
    plan.status = "succeeded"
    incident = session.get(Incident, plan.incident_id)
    incident.status = "investigating"
    incident.active_alerts = alerts
    execution = MitigationExecution(
        organization_id="org_local", plan_id=plan.id, service="shop-demo", environment="local",
        status="succeeded", before_state={"reversible": False}, result={"restarted": True},
        finished_at=utc_now(),
    )
    session.add(execution)
    session.flush()
    session.add(Evidence(
        organization_id="org_local", incident_id=incident.id, collection_id=uuid.uuid4(),
        kind="metric", source="prometheus", title="5xx error ratio peaked",
        content={"metric": "error_ratio", "threshold": 0.05, "breached": True, "peak": 0.4},
        relevance=0.8,
    ))
    session.commit()
    return incident, plan, execution


def run_now(session, settings, incident, execution, http):
    settings.recovery_window_seconds = 0
    settings.prometheus_url = "http://prom"
    job = maybe_enqueue_verify(session, settings, incident, execution)
    job.status = "running"
    session.commit()
    return run_verify(session, settings, str(execution.id), job.payload["run_id"], http=http, payload=job.payload)


def test_healthy_recovery_mitigates_the_incident(session, settings):
    incident, plan, execution = setup_succeeded(session)
    out = run_now(session, settings, incident, execution, prom_value(0.01))
    assert out["status"] == "healthy"
    session.refresh(incident)
    session.refresh(plan)
    check = session.scalars(select(RecoveryCheck)).one()
    assert incident.status == "mitigated" and plan.status == "succeeded"
    assert check.verdict == "healthy" and check.signals[0]["ok"] is True
    assert check.signals[0]["before"] == 0.4 and check.signals[0]["after"] == 0.01
    events = [e.message for e in session.scalars(select(IncidentEvent))]
    assert any("Recovered after restart_service" in m for m in events)
    run = session.scalars(select(PipelineRun).where(PipelineRun.trigger_kind == "recovery_verification")).one()
    assert [s.stage for s in run.steps] == ["queued", "waiting", "signals", "verdict"]
    assert "undo" not in [s.stage for s in run.steps]


def test_unhealthy_recovery_undoes_and_reinvestigates(session, settings):
    incident, plan, execution = setup_succeeded(session, key="INC-3002")
    out = run_now(session, settings, incident, execution, prom_value(0.4))
    assert out["status"] == "unhealthy" and out["undone"] is True
    session.refresh(incident)
    session.refresh(plan)
    session.refresh(execution)
    assert execution.status == "undone" and plan.status == "failed"
    assert incident.status == "investigating"
    events = [e.message for e in session.scalars(select(IncidentEvent))]
    assert any("Not recovered; restart_service undone" in m for m in events)
    jobs = session.scalars(select(Job).where(Job.type == "rca.investigate")).all()
    assert jobs and jobs[-1].payload["incident_id"] == str(incident.id)
    run = session.scalars(select(PipelineRun).where(PipelineRun.trigger_kind == "recovery_verification")).one()
    assert [s.stage for s in run.steps] == ["queued", "waiting", "signals", "verdict", "undo"]


def test_inconclusive_recovery_does_not_undo(session, settings):
    incident, plan, execution = setup_succeeded(session, key="INC-3003")
    out = run_now(session, settings, incident, execution, prom_down())
    assert out["status"] == "inconclusive"
    session.refresh(incident)
    session.refresh(plan)
    session.refresh(execution)
    assert execution.status == "succeeded" and plan.status == "succeeded"
    assert incident.status == "investigating"
    events = [e.message for e in session.scalars(select(IncidentEvent))]
    assert any("inconclusive" in m.lower() for m in events)
    assert session.scalars(select(Job).where(Job.type == "rca.investigate")).all() == []
    run = session.scalars(select(PipelineRun).where(PipelineRun.trigger_kind == "recovery_verification")).one()
    assert "undo" not in [s.stage for s in run.steps]


def test_verify_reschedules_until_the_window_ends(session, settings):
    incident, plan, execution = setup_succeeded(session, key="INC-3004")
    settings.recovery_window_seconds = 60
    settings.recovery_poll_seconds = 15
    job = maybe_enqueue_verify(session, settings, incident, execution)
    job.status = "running"
    session.commit()
    out = run_verify(session, settings, str(execution.id), job.payload["run_id"], payload=job.payload)
    assert out["status"] == "waiting"
    nxt = session.scalars(select(Job).where(Job.type == "recovery.verify", Job.status == "queued")).one()
    assert nxt.run_after is not None and nxt.run_after > nxt.created_at
    run = session.scalars(select(PipelineRun).where(PipelineRun.trigger_kind == "recovery_verification")).one()
    assert [s.stage for s in run.steps] == ["queued", "waiting"]
    assert run.status == "running"
