from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.db.models import Service, ServiceDeployment
from app.modules.catalog.service import seed_from_file
from app.modules.mitigation.adapters import (
    ActionPlan,
    DisableFeatureAdapter,
    RestartServiceAdapter,
    RollbackReleaseAdapter,
)


def add_release_history(session) -> Service:
    service = Service(
        organization_id="org_local",
        name="shop-demo",
        environment="local",
        telemetry_service_name="shop-demo",
    )
    session.add(service)
    session.flush()
    now = datetime.now(timezone.utc)
    session.add_all([
        ServiceDeployment(
            organization_id="org_local",
            service_id=service.id,
            version="v1.0.0",
            commit_sha="healthy",
            description="Initial healthy release",
            changed_files=[],
            source="catalog_seed",
            deployed_at=now - timedelta(hours=1),
        ),
        ServiceDeployment(
            organization_id="org_local",
            service_id=service.id,
            version="v1.1.0",
            commit_sha="broken",
            description="Tax refactor (test bench: introduces planted bug code_null_tax).",
            changed_files=["app/pricing.py"],
            source="test_bench",
            deployed_at=now,
        ),
    ])
    session.commit()
    return service


def stateful_shop(initial: dict[str, bool] | None = None):
    flags = initial or {"code_null_tax": True, "db_pool_leak": True}
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/admin/bugs":
            return httpx.Response(200, json={name: {"enabled": enabled} for name, enabled in flags.items()})
        if request.url.path == "/admin/restart":
            return httpx.Response(
                200,
                json={"restarted": True, "before": {"pool_in_use": 5}, "after": {"pool_in_use": 0}},
            )
        parts = request.url.path.split("/")
        if len(parts) == 5 and parts[1:3] == ["admin", "bugs"]:
            flag, action = parts[3], parts[4]
            if flag not in flags:
                return httpx.Response(404)
            flags[flag] = action == "enable"
            return httpx.Response(200, json={"name": flag, "enabled": flags[flag]})
        return httpx.Response(404)

    return flags, calls, httpx.Client(transport=httpx.MockTransport(handler))


def plan(**params) -> ActionPlan:
    return ActionPlan(
        organization_id="org_local",
        service="shop-demo",
        environment="local",
        params=params,
    )


def test_catalog_actions_are_seeded_into_service_config(session, settings, tmp_path):
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        """services:
  - name: shop-demo
    environment: local
    telemetry_service_name: shop-demo
    actions:
      - type: restart_service
        title: Restart
        risk: medium
        approval: single
        params: {}
        preconditions: [no_execution_running, restart_rate_limit]
"""
    )
    settings.service_catalog_file = str(catalog)

    service = seed_from_file(session, settings)[0]

    assert service.config["actions"][0]["type"] == "restart_service"
    assert service.config["actions"][0]["preconditions"] == ["no_execution_running", "restart_rate_limit"]


def test_disable_feature_execute_and_undo_restore_exact_state(session, settings):
    flags, calls, client = stateful_shop()
    adapter = DisableFeatureAdapter(session, settings, client)

    result = adapter.execute(plan(flag="code_null_tax"))
    assert result.ok and result.before_state == {"flag": "code_null_tax", "enabled": True}
    assert flags["code_null_tax"] is False

    undone = adapter.undo(result)
    assert undone.ok and flags["code_null_tax"] is True
    assert ("POST", "/admin/bugs/code_null_tax/enable") in calls

    flags["code_null_tax"] = False
    already_off = adapter.execute(plan(flag="code_null_tax"))
    calls_before_undo = len(calls)
    assert adapter.undo(already_off).ok and flags["code_null_tax"] is False
    assert len(calls) == calls_before_undo


def test_rollback_execute_and_undo_record_deployments(session, settings):
    add_release_history(session)
    flags, _, client = stateful_shop()
    adapter = RollbackReleaseAdapter(session, settings, client)

    assert adapter.preflight(plan()).ok
    result = adapter.execute(plan())
    assert result.ok and result.before_state["current_version"] == "v1.1.0"
    assert result.result["version"] == "v1.0.0" and flags["code_null_tax"] is False

    undone = adapter.undo(result)
    assert undone.ok and undone.result["version"] == "v1.1.0"
    assert flags["code_null_tax"] is True
    deployments = session.scalars(
        select(ServiceDeployment).order_by(ServiceDeployment.deployed_at)
    ).all()
    assert [deployment.version for deployment in deployments][-2:] == ["v1.0.0", "v1.1.0"]
    assert "rollback to v1.0.0" in deployments[-2].description
    assert "roll-forward to v1.1.0" in deployments[-1].description


def test_restart_execute_and_undo_is_explicitly_not_reversible(session, settings):
    add_release_history(session)
    _, calls, client = stateful_shop()
    adapter = RestartServiceAdapter(session, settings, client)

    assert adapter.preflight(plan()).ok
    result = adapter.execute(plan())
    assert result.ok and result.before_state["remote"]["pool_in_use"] == 5
    assert ("POST", "/admin/restart") in calls

    undone = adapter.undo(result)
    assert undone.ok and undone.result == {"reversible": False, "changed": False}
    assert "not reversible" in undone.message


@pytest.mark.parametrize("adapter_type", [
    DisableFeatureAdapter,
    RollbackReleaseAdapter,
    RestartServiceAdapter,
])
def test_adapter_timeout_returns_typed_error(session, settings, adapter_type):
    if adapter_type is RollbackReleaseAdapter:
        add_release_history(session)

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    adapter = adapter_type(session, settings, httpx.Client(transport=httpx.MockTransport(timeout)))
    result = adapter.execute(plan(flag="code_null_tax"))

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "timeout" and result.error.retryable is True
