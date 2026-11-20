import asyncio

import httpx
import pytest

from app.core.config import Settings
from app.modules.system_checks.contracts import CheckResult, overall_status
from app.modules.system_checks.service import SystemChecksService

TOKEN = "super-secret-token-123"


def make_settings(**kw) -> Settings:
    base = dict(
        otel_collector_health_url="http://otel:13133",
        tempo_url="http://tempo:3200",
        loki_url="http://loki:3100",
        prometheus_url="http://prom:9090",
        alertmanager_url="http://am:9093",
        shop_demo_url="http://shop:8080",
        alertmanager_webhook_token=TOKEN,
        check_timeout_seconds=0.5,
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


def healthy_handler(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if path in ("/", "/ready", "/-/ready", "/health"):
        return httpx.Response(200, text="ready")
    if host == "tempo" and path == "/api/search":
        return httpx.Response(200, json={"traces": [{"traceID": "a"}, {"traceID": "b"}]})
    if host == "loki" and path.endswith("query_range"):
        return httpx.Response(200, json={"data": {"result": [{"values": [["1", "x"], ["2", "y"]]}]}})
    if host == "prom" and path == "/api/v1/query":
        q = request.url.params["query"]
        val = "0.5" if "5.." in q else "2.0"
        return httpx.Response(200, json={"data": {"result": [{"value": [0, val]}]}})
    if host == "am" and path == "/api/v2/alerts":
        return httpx.Response(200, json=[{"status": {"state": "active"}}])
    if host == "shop" and path.startswith("/admin/bugs"):
        return httpx.Response(200, json={"enabled": True})
    if host == "shop" and path == "/admin/traffic":
        return httpx.Response(200, json={"status_counts": {"200": 30, "500": 20}})
    return httpx.Response(404)


def service(handler, **kw) -> SystemChecksService:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SystemChecksService(make_settings(**kw), client)


def by_id(report):
    return {r.id: r for r in report.components + report.signals}


@pytest.mark.asyncio
async def test_all_healthy():
    report = await service(healthy_handler).run_all()
    assert report.overall == "success"
    results = by_id(report)
    assert len(report.components) == 6 and len(report.signals) == 4
    assert results["signal-traces"].detail.startswith("2 error trace")
    assert results["signal-logs"].detail.startswith("2 ERROR log")
    assert "25.0%" in results["signal-metrics"].detail


@pytest.mark.asyncio
async def test_no_signals_yet_are_warnings():
    def handler(request):
        host, path = request.url.host, request.url.path
        if host == "tempo" and path == "/api/search":
            return httpx.Response(200, json={"traces": []})
        if host == "loki" and path.endswith("query_range"):
            return httpx.Response(200, json={"data": {"result": []}})
        if host == "prom" and path == "/api/v1/query":
            return httpx.Response(200, json={"data": {"result": []}})
        if host == "am" and path == "/api/v2/alerts":
            return httpx.Response(200, json=[])
        return healthy_handler(request)

    report = await service(handler).run_all()
    assert report.overall == "warning"
    assert all(s.status == "warning" for s in report.signals)


@pytest.mark.asyncio
async def test_one_failing_component_does_not_break_others():
    def handler(request):
        if request.url.host == "loki":
            raise httpx.ConnectError("refused", request=request)
        return healthy_handler(request)

    report = await service(handler).run_all()
    results = by_id(report)
    assert results["loki"].status == "failed"
    assert "connection refused" in results["loki"].detail
    assert results["signal-logs"].status == "failed"
    assert results["tempo"].status == "success"
    assert results["signal-traces"].status == "success"
    assert report.overall == "failed"


@pytest.mark.asyncio
async def test_http_500_and_malformed_body():
    def handler(request):
        if request.url.host == "prom" and request.url.path == "/-/ready":
            return httpx.Response(500)
        if request.url.host == "tempo" and request.url.path == "/api/search":
            return httpx.Response(200, text="not json")
        return healthy_handler(request)

    results = by_id(await service(handler).run_all())
    assert results["prometheus"].status == "failed" and results["prometheus"].detail == "HTTP 500"
    assert results["signal-traces"].status == "failed"


@pytest.mark.asyncio
async def test_timeout_becomes_failed_check():
    async def slow(request):
        if request.url.host == "tempo":
            await asyncio.sleep(5)
        return healthy_handler(request)

    results = by_id(await service(slow).run_all())
    assert results["tempo"].status == "failed"
    assert results["tempo"].detail == "timed out"
    assert results["alertmanager"].status == "success"


def test_shop_demo_url_defaults_to_docker_port():
    # The default only fits the Docker stack; local runs on another port must set SHOP_DEMO_URL.
    assert Settings(_env_file=None).shop_demo_url == "http://localhost:8080"


@pytest.mark.asyncio
async def test_shop_demo_check_uses_url_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("SHOP_DEMO_URL", raising=False)
    env = tmp_path / ".env"
    env.write_text("SHOP_DEMO_URL=http://localhost:18080\n")
    settings = Settings(_env_file=env)
    assert settings.shop_demo_url == "http://localhost:18080"

    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.port == 18080 and request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    svc = SystemChecksService(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = await svc._component("shop-demo", "shop-demo service", svc._component_targets()[-1][2])
    assert "http://localhost:18080/health" in seen
    assert result.status == "success" and result.detail == "ready (HTTP 200)"


@pytest.mark.asyncio
async def test_port_held_by_silent_process_is_timed_out():
    # Something accepts the connection but never answers HTTP (what port 8080 did here).
    def handler(request):
        if request.url.host == "shop" and request.url.path == "/health":
            raise httpx.ReadTimeout("no response", request=request)
        return healthy_handler(request)

    results = by_id(await service(handler).run_all())
    assert results["shop-demo"].status == "failed"
    assert results["shop-demo"].detail == "timed out"
    assert results["tempo"].status == "success"


@pytest.mark.asyncio
async def test_secret_never_in_detail():
    def handler(request):
        if request.url.host == "am":
            raise RuntimeError(f"bad auth with token {TOKEN}")
        return healthy_handler(request)

    report = await service(handler).run_all()
    for r in report.components + report.signals:
        assert TOKEN not in r.detail
    assert "***" in by_id(report)["alertmanager"].detail


@pytest.mark.asyncio
async def test_actions():
    svc = service(healthy_handler)
    enabled = await svc.run_action("enable_bug")
    assert enabled.status == "success" and "enabled" in enabled.detail
    traffic = await svc.run_action("generate_traffic", requests=50)
    assert traffic.status == "warning" and "500: 20" in traffic.detail
    unknown = await svc.run_action("drop_database")
    assert unknown.status == "failed"


@pytest.mark.asyncio
async def test_action_failure_when_shop_demo_down():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    result = await service(handler).run_action("enable_bug")
    assert result.status == "failed"


def test_overall_status_rules():
    def r(s):
        return CheckResult(id="x", name="x", category="component", status=s)

    assert overall_status([r("success"), r("warning")]) == "warning"
    assert overall_status([r("success"), r("failed"), r("warning")]) == "failed"
    assert overall_status([r("success")]) == "success"
    assert overall_status([r("skipped")]) == "skipped"


@pytest.mark.asyncio
async def test_load_actions():
    def handler(request):
        if request.url.path in ("/admin/load/start", "/admin/load/stop"):
            return httpx.Response(200, json={"ok": True})
        return healthy_handler(request)

    svc = service(handler)
    assert (await svc.run_action("start_load")).status == "success"
    assert (await svc.run_action("stop_load")).status == "success"


@pytest.mark.asyncio
async def test_readiness_503_is_warning_not_failure():
    def handler(request):
        if request.url.host == "loki" and request.url.path == "/ready":
            return httpx.Response(503, text="Ingester not ready")
        return healthy_handler(request)

    results = by_id(await service(handler).run_all())
    assert results["loki"].status == "warning"
    assert "not ready yet" in results["loki"].detail
