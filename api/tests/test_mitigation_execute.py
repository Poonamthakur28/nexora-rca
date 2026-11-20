import httpx
from sqlalchemy import select

from app.db.models import (
    Incident,
    IncidentEvent,
    Job,
    MitigationExecution,
    MitigationPlan,
    PipelineRun,
    RecoveryCheck,
    Service,
)
from app.modules.mitigation.execute import run_execute
from tests.conftest import drain
from tests.test_mitigation_policy import SHOP_ACTIONS, attach_actions


def mock_shop(restarted: bool = True):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/restart" and restarted:
            return httpx.Response(
                200, json={"restarted": True, "before": {"pool_in_use": 4}, "after": {"pool_in_use": 0}},
            )
        raise httpx.ReadTimeout("too slow", request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


def make_plan(session, key="INC-2001", action="restart_service") -> MitigationPlan:
    service = session.scalars(select(Service)).first()
    if service is None:
        service = Service(
            organization_id="org_local", name="shop-demo", environment="local",
            telemetry_service_name="shop-demo", config={"actions": SHOP_ACTIONS},
        )
        session.add(service)
        session.flush()
    else:
        attach_actions(session)
    inc = Incident(
        organization_id="org_local", key=key, title="5xx", service="shop-demo",
        environment="local", severity="critical",
    )
    session.add(inc)
    session.flush()
    plan = MitigationPlan(
        organization_id="org_local", incident_id=inc.id, action_type=action,
        title="Restart the service connection pool", risk="medium", approval_mode="single",
        status="proposed", score=0.6, params={},
        gate_results=[
            {"gate": "no_execution_running", "ok": True, "detail": "ok"},
            {"gate": "restart_rate_limit", "ok": True, "detail": "0 restarts"},
        ],
        undo_plan={"action": "none", "description": "Restart is not reversible."},
        rationale="test plan",
    )
    session.add(plan)
    session.commit()
    return plan


def test_approve_executes_once(client, factory, session, settings, monkeypatch):
    plan = make_plan(session)
    transport_client = mock_shop()
    monkeypatch.setattr("app.modules.mitigation.adapters.httpx.Client", lambda *args, **kwargs: transport_client)

    r = client.post(f"/api/v1/mitigation-plans/{plan.id}/approve", json={"comment": "safe"},
                    headers={"X-Nexora-Actor": "alice"})
    assert r.status_code == 202 and r.json()["queued"] is True
    assert client.post(f"/api/v1/mitigation-plans/{plan.id}/approve", json={"comment": "again"}).status_code == 409

    drain(factory, settings)
    with factory() as s:
        row = s.get(MitigationPlan, plan.id)
        runs = s.scalars(select(MitigationExecution).where(MitigationExecution.plan_id == plan.id)).all()
        jobs = s.scalars(select(Job).where(Job.type == "mitigation.execute")).all()
        events = [e.message for e in s.scalars(select(IncidentEvent).where(IncidentEvent.incident_id == row.incident_id))]
        pipeline = s.scalars(select(PipelineRun).where(PipelineRun.trigger_kind == "mitigation_execution")).one()
        stages = [step.stage for step in pipeline.steps]
        plan_status, decided_by = row.status, row.decided_by
        run_status = runs[0].status if runs else None
        job_status, job_attempts = (jobs[0].status, jobs[0].attempts) if jobs else (None, None)
    assert plan_status == "succeeded" and decided_by == "alice"
    assert len(runs) == 1 and run_status == "succeeded"
    assert len(jobs) == 1 and job_status == "succeeded" and job_attempts == 1
    assert any("Executed restart_service (approved by alice)" in m for m in events)
    assert stages == ["queued", "lock", "preflight", "execute", "stored"]

    listed = client.get(f"/api/v1/incidents/{plan.incident_id}/mitigation-plans").json()
    assert listed[0]["status"] == "succeeded"
    execs = client.get(f"/api/v1/mitigation-plans/{plan.id}/executions").json()
    assert execs[0]["status"] == "succeeded"


def test_double_approve_is_409(client, session):
    plan = make_plan(session, key="INC-2002")
    first = client.post(f"/api/v1/mitigation-plans/{plan.id}/approve", json={"comment": ""})
    assert first.status_code == 202
    second = client.post(f"/api/v1/mitigation-plans/{plan.id}/approve", json={"comment": ""})
    assert second.status_code == 409
    assert second.json()["detail"]["error"] == "not_proposed"


def test_running_execution_blocks_the_lock(session, settings):
    held = make_plan(session, key="INC-2003")
    session.add(MitigationExecution(
        organization_id="org_local", plan_id=held.id, service="shop-demo", environment="local",
        status="running",
    ))
    other = make_plan(session, key="INC-2004")
    other.status = "approved"
    session.commit()

    out = run_execute(session, settings, str(other.id), http=mock_shop())
    assert out["status"] == "failed" and "already running" in out["error"]
    session.refresh(other)
    assert other.status == "failed"
    assert session.scalars(
        select(MitigationExecution).where(MitigationExecution.plan_id == other.id)
    ).first() is None


def test_execute_failure_does_not_retry(client, factory, session, settings):
    plan = make_plan(session, key="INC-2005")
    r = client.post(f"/api/v1/mitigation-plans/{plan.id}/approve", json={"comment": "go"})
    assert r.status_code == 202

    drain(factory, settings)
    with factory() as s:
        row = s.get(MitigationPlan, plan.id)
        runs = s.scalars(select(MitigationExecution).where(MitigationExecution.plan_id == plan.id)).all()
        jobs = s.scalars(select(Job).where(Job.type == "mitigation.execute")).all()
        queued = s.scalars(select(Job).where(Job.type == "mitigation.execute", Job.status == "queued")).all()
    assert row.status == "failed"
    assert len(runs) == 1 and runs[0].status == "failed" and runs[0].error
    assert len(jobs) == 1 and jobs[0].attempts == 1 and jobs[0].status == "succeeded"
    assert queued == []


def test_mitigation_bundle_for_the_ui(client, session):
    plan = make_plan(session, key="INC-2101")
    session.add(MitigationPlan(
        organization_id="org_local", incident_id=plan.incident_id,
        action_type="disable_feature", title="Disable a planted failure flag",
        risk="low", approval_mode="single", status="blocked", score=0,
        params={}, gate_results=[{"gate": "flag_exists", "ok": False, "detail": "no flag in the request"}],
        undo_plan={}, rationale="Blocked: no flag",
    ))
    session.commit()
    body = client.get(f"/api/v1/incidents/{plan.incident_id}/mitigation").json()
    assert body["chosen"]["id"] == str(plan.id)
    assert body["chosen"]["target"] == "shop-demo · local"
    assert body["blocked"][0]["gate_results"][0]["ok"] is False
    assert body["pending"] is False and body["recovery"] is None


def test_mitigation_bundle_recovery_and_no_safe_action(client, session):
    plan = make_plan(session, key="INC-2102")
    plan.status = "succeeded"
    execution = MitigationExecution(
        organization_id="org_local", plan_id=plan.id, service="shop-demo", environment="local",
        status="succeeded",
    )
    session.add(execution)
    session.flush()
    session.add(RecoveryCheck(
        organization_id="org_local", execution_id=execution.id, window_seconds=120,
        signals=[{"metric": "error_ratio", "threshold": 0.05, "before": 0.4, "after": 0.01, "ok": True}],
        verdict="healthy",
    ))
    session.commit()
    body = client.get(f"/api/v1/incidents/{plan.incident_id}/mitigation").json()
    assert body["recovery"]["verdict"] == "healthy"
    assert body["executions"][0]["recovery"]["signals"][0]["after"] == 0.01
    assert body["pending"] is False

    other = Incident(
        organization_id="org_local", key="INC-2103", title="dependency", service="shop-demo",
        environment="local", severity="high",
    )
    session.add(other)
    session.flush()
    session.add(IncidentEvent(
        organization_id="org_local", incident_id=other.id, kind="no_safe_action",
        message="no safe action; escalate to on-call",
    ))
    session.commit()
    empty = client.get(f"/api/v1/incidents/{other.id}/mitigation").json()
    assert empty["chosen"] is None
    assert "escalate to on-call" in empty["no_safe_action"]
