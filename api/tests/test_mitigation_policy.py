from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from app.db.models import IncidentEvent, MitigationPlan, PipelineRun, Service
from app.modules.mitigation.policy import plan_for
from app.modules.rca.engine import investigate
from tests.conftest import drain
from tests.test_evidence import make_bench, trace_body, urls
from tests.test_rca import collected

SHOP_ACTIONS = [
    {
        "type": "rollback_release",
        "title": "Roll back to the previous healthy release",
        "risk": "low",
        "approval": "single",
        "params": {},
        "preconditions": [
            "previous_release_exists",
            "previous_release_healthy",
            "no_irreversible_migration",
            "no_execution_running",
        ],
    },
    {
        "type": "disable_feature",
        "title": "Disable a planted failure flag",
        "risk": "low",
        "approval": "single",
        "params": {"flag": None},
        "preconditions": ["flag_exists", "no_execution_running"],
    },
    {
        "type": "restart_service",
        "title": "Restart the service connection pool",
        "risk": "medium",
        "approval": "single",
        "params": {},
        "preconditions": ["no_execution_running", "restart_rate_limit"],
    },
]


def attach_actions(session) -> Service:
    service = session.scalars(select(Service)).one()
    service.config = {**(service.config or {}), "actions": SHOP_ACTIONS}
    session.commit()
    return service


def test_null_tax_incident_chooses_rollback_release(session, settings, tmp_path, factory):
    inc = collected(session, settings, tmp_path)
    attach_actions(session)
    drain(factory, settings)

    plans = session.scalars(select(MitigationPlan).where(MitigationPlan.incident_id == inc.id)).all()
    proposed = [p for p in plans if p.status == "proposed"]
    assert len(proposed) == 1 and proposed[0].action_type == "rollback_release"
    assert proposed[0].risk == "low" and proposed[0].approval_mode == "single"
    assert all(g["ok"] for g in proposed[0].gate_results)
    assert "other: not in the catalog" in proposed[0].rationale
    assert proposed[0].undo_plan["action"] == "roll_forward"

    run = session.scalars(select(PipelineRun).where(PipelineRun.trigger_kind == "mitigation_plan")).one()
    assert [s.stage for s in run.steps] == ["queued", "options", "gates", "choose", "stored"]
    assert run.status == "success"


def test_slow_gateway_incident_has_no_safe_plan(session, settings, tmp_path, factory):
    ns = str(int(datetime.now(timezone.utc).timestamp() * 1e9))

    def handler(request):
        host, path = request.url.host, request.url.path
        if host == "tempo" and path == "/api/search":
            q = request.url.params["q"]
            traces = [{"traceID": "slow1", "durationMs": 2600}] if "duration" in q else []
            return httpx.Response(200, json={"traces": traces})
        if host == "tempo":
            body = trace_body(ns, gateway_seconds=2.5)
            for rs in body["trace"]["resourceSpans"]:
                for ss in rs["scopeSpans"]:
                    for sp in ss["spans"]:
                        sp.pop("events", None)
                        sp.get("status", {}).pop("code", None)
            return httpx.Response(200, json=body)
        if host == "loki":
            return httpx.Response(200, json={"data": {"result": []}})
        if host == "prom":
            t = datetime.now(timezone.utc).timestamp()
            return httpx.Response(200, json={"data": {"result": [{"values": [[t - 60, "0"], [t, "0"]]}]}})
        return httpx.Response(404)

    inc = make_bench(session, settings, tmp_path, minutes_before=600)
    attach_actions(session)
    urls(settings)
    from app.modules.evidence.collect import collect_evidence

    collect_evidence(session, settings, str(inc.id), http=httpx.Client(transport=httpx.MockTransport(handler)))
    drain(factory, settings)

    plans = session.scalars(select(MitigationPlan).where(MitigationPlan.incident_id == inc.id)).all()
    assert plans == []
    events = [e.message for e in session.scalars(select(IncidentEvent).where(IncidentEvent.incident_id == inc.id))]
    assert any("no safe action; escalate to on-call" in m for m in events)
    assert any("failover: not in the catalog" in m for m in events)


def test_running_execution_blocks_a_new_plan(session, settings, tmp_path):
    inc = collected(session, settings, tmp_path)
    attach_actions(session)
    investigate(session, settings, str(inc.id))

    session.add(MitigationPlan(
        organization_id=inc.organization_id, incident_id=inc.id, action_type="restart_service",
        title="in flight", risk="medium", approval_mode="single", status="executing", score=0,
        params={}, gate_results=[], undo_plan={}, rationale="already running",
    ))
    session.commit()

    chosen = plan_for(session, settings, inc)
    assert chosen is None
    blocked = [p for p in session.scalars(select(MitigationPlan)).all()
               if p.status == "blocked" and p.action_type == "rollback_release"]
    assert blocked
    failed = {g["gate"]: g for g in blocked[0].gate_results if not g["ok"]}
    assert "no_execution_running" in failed
    assert "already running" in failed["no_execution_running"]["detail"]


def test_blocked_plans_include_failed_gates(session, settings, tmp_path):
    inc = collected(session, settings, tmp_path)
    attach_actions(session)
    from app.db.models import ServiceDeployment

    svc = session.scalars(select(Service)).one()
    current = session.scalars(
        select(ServiceDeployment).where(ServiceDeployment.service_id == svc.id)
        .order_by(ServiceDeployment.deployed_at.desc())
    ).first()
    current.has_irreversible_migration = True
    session.commit()

    investigate(session, settings, str(inc.id))
    chosen = plan_for(session, settings, inc)
    assert chosen is None
    rollback = session.scalars(
        select(MitigationPlan).where(MitigationPlan.action_type == "rollback_release")
    ).one()
    assert rollback.status == "blocked"
    failed = {g["gate"]: g["ok"] for g in rollback.gate_results}
    assert failed["no_irreversible_migration"] is False
    assert failed["previous_release_exists"] is True
    assert "irreversible migration" in rollback.rationale


# ---------- rollback target safety (INC-1001 regression) ----------

from datetime import timedelta  # noqa: E402

from app.db.models import Incident, RcaReport, ServiceDeployment  # noqa: E402
from app.modules.mitigation.gates import MIN_OBSERVED_MINUTES, previous_release_healthy  # noqa: E402

ROLLBACK_ONLY = [a for a in SHOP_ACTIONS if a["type"] == "rollback_release"]


def history(session, releases: list[tuple[str, int]], detected_min_ago: int, key: str = "INC-1001",
            trigger: dict | None = None) -> Incident:
    """Service with releases [(version, minutes ago)], an incident and its RCA (trigger as given)."""
    now = datetime.now(timezone.utc)
    svc = Service(organization_id="org_local", name="shop-demo", environment="local",
                  telemetry_service_name="shop-demo", config={"actions": ROLLBACK_ONLY})
    session.add(svc)
    session.flush()
    for version, ago in releases:
        session.add(ServiceDeployment(organization_id="org_local", service_id=svc.id, version=version,
                                      deployed_at=now - timedelta(minutes=ago), changed_files=["app/pricing.py"],
                                      source="test_bench"))
    inc = Incident(organization_id="org_local", key=key, title="5xx", service="shop-demo", environment="local",
                   severity="critical", detected_at=now - timedelta(minutes=detected_min_ago))
    session.add(inc)
    session.flush()
    session.add(RcaReport(
        organization_id="org_local", incident_id=inc.id, provider="heuristic", model="rules-v1",
        status="succeeded", category="code", confidence=0.85, grounded=True,
        report={"summary": "AttributeError at app/pricing.py:32", "confidence": 0.85,
                "root_cause": {"category": "code", "file": "app/pricing.py", "line": 32},
                "trigger": trigger if trigger is not None else {"kind": "unknown"},
                "mitigation_options": [{"action": "rollback", "target": "shop-demo", "reason": "release", "risk": "low"}]},
    ))
    session.commit()
    return inc


def gate(session, inc) -> dict:
    svc = session.scalars(select(Service)).one()
    return previous_release_healthy(session, inc, svc, ROLLBACK_ONLY[0])


def test_rollback_to_the_rca_blamed_release_is_blocked(session, settings):
    # INC-1001: v1.4.0 broke checkout, v1.5.0 fixed it; rolling back would reinstall the bug.
    inc = history(session, [("v1.0.0", 120), ("v1.4.0", 40), ("v1.5.0", 20)], detected_min_ago=25,
                  trigger={"kind": "release", "release_version": "v1.4.0"})
    assert plan_for(session, settings, inc) is None
    plan = session.scalars(select(MitigationPlan)).one()
    assert plan.status == "blocked" and plan.action_type == "rollback_release"
    failed = {g["gate"]: g["detail"] for g in plan.gate_results if not g["ok"]}
    assert failed["previous_release_healthy"] == "v1.4.0 is the release the RCA blames for INC-1001"
    events = [e.message for e in session.scalars(select(IncidentEvent).where(IncidentEvent.incident_id == inc.id))]
    assert any("no safe action; escalate to on-call" in m for m in events)


def test_rollback_to_a_verified_healthy_release_is_proposed(session, settings):
    # v1.1.0 introduced the bug 3 min ago; v1.0.0 ran 2 h before it with no incident.
    inc = history(session, [("v1.0.0", 120), ("v1.1.0", 3)], detected_min_ago=1,
                  trigger={"kind": "release", "release_version": "v1.1.0"})
    chosen = plan_for(session, settings, inc)
    assert chosen is not None and chosen.action_type == "rollback_release"
    assert chosen.status == "proposed" and chosen.approval_mode == "single"  # still needs a human
    detail = {g["gate"]: g for g in chosen.gate_results}["previous_release_healthy"]
    assert detail["ok"] and detail["detail"] == "v1.0.0 ran 117 min with no incident before v1.1.0"


def test_rca_release_without_a_version_blocks_rollback(session):
    inc = history(session, [("v1.0.0", 120), ("v1.1.0", 3)], detected_min_ago=1,
                  trigger={"kind": "release", "release_version": None})
    result = gate(session, inc)
    assert not result["ok"] and "does not name it" in result["detail"]


def test_rca_release_that_is_not_recorded_blocks_rollback(session):
    inc = history(session, [("v1.0.0", 120), ("v1.1.0", 3)], detected_min_ago=1,
                  trigger={"kind": "release", "release_version": "v9.9.9"})
    result = gate(session, inc)
    assert not result["ok"] and "v9.9.9, which is not a recorded release" in result["detail"]


def test_rollback_that_does_not_remove_the_blamed_release_is_blocked(session):
    # Blamed v1.4.0 is older than the release being rolled back; v1.5.0 may still contain it.
    inc = history(session, [("v1.4.0", 120), ("v1.5.0", 60), ("v1.6.0", 3)], detected_min_ago=1,
                  trigger={"kind": "release", "release_version": "v1.4.0"})
    result = gate(session, inc)
    assert not result["ok"]
    assert result["detail"] == ("the RCA blames v1.4.0, but the current release is v1.6.0; "
                                "rolling back to v1.5.0 does not remove v1.4.0")


def test_current_incident_on_the_target_release_blocks_rollback(session):
    # No release blamed: the incident itself was detected while v1.4.0 was live, so v1.4.0 is not healthy.
    inc = history(session, [("v1.0.0", 120), ("v1.4.0", 40), ("v1.5.0", 20)], detected_min_ago=25)
    result = gate(session, inc)
    assert not result["ok"] and result["detail"] == "v1.4.0 had incident(s) INC-1001"


def test_current_incident_after_the_current_release_does_not_count_against_the_target(session):
    inc = history(session, [("v1.0.0", 120), ("v1.1.0", 30)], detected_min_ago=10)
    result = gate(session, inc)
    assert result["ok"] and "v1.0.0 ran 90 min with no incident" in result["detail"]


def test_absence_of_incidents_is_not_proof_for_a_short_lived_release(session):
    inc = history(session, [("v1.0.0", 120), ("v1.1.0", 12), ("v1.2.0", 10)], detected_min_ago=1)
    result = gate(session, inc)
    assert not result["ok"]
    assert result["detail"].startswith("v1.1.0 was live only 2.0 min before v1.2.0")
    assert f"at least {MIN_OBSERVED_MINUTES} min" in result["detail"]


def test_approval_rechecks_and_refuses_a_now_unsafe_rollback(client, session, settings):
    # A rollback proposed while v1.4.0 was current becomes unsafe once the fix v1.5.0 is recorded.
    inc = history(session, [("v1.0.0", 120), ("v1.4.0", 40)], detected_min_ago=25,
                  trigger={"kind": "release", "release_version": "v1.4.0"})
    plan = plan_for(session, settings, inc)
    assert plan is not None and plan.status == "proposed"
    session.commit()
    svc = session.scalars(select(Service)).one()
    session.add(ServiceDeployment(organization_id="org_local", service_id=svc.id, version="v1.5.0",
                                  deployed_at=datetime.now(timezone.utc), changed_files=["app/pricing.py"]))
    session.commit()

    r = client.post(f"/api/v1/mitigation-plans/{plan.id}/approve", json={"comment": "ok"},
                    headers={"X-Nexora-Actor": "alice"})
    assert r.status_code == 409
    assert "v1.4.0 is the release the RCA blames for INC-1001" in r.text
    session.refresh(plan)
    assert plan.status != "approved" and not session.scalars(
        select(MitigationPlan).where(MitigationPlan.status == "executing")).all()
