import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.db.models import Evidence, Incident, IncidentEvent, PipelineRun, Service, ServiceDeployment
from app.modules.catalog.service import (
    current_deployment,
    next_version,
    record_bench_release,
    record_deployment,
    seed_from_file,
)
from app.modules.evidence.collect import collect_evidence
from app.modules.evidence.redaction import redact, redact_obj
from app.modules.evidence.stacktrace import deepest_app_frame, exception_line, parse_frames

STACK = """Traceback (most recent call last):
  File "/usr/local/lib/python3.12/site-packages/starlette/middleware/base.py", line 148, in call_next
    message = await recv_stream.receive()
anyio.EndOfStream

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/srv/app/main.py", line 40, in trace_requests
    response = await call_next(request)
  File "/usr/local/lib/python3.12/site-packages/starlette/middleware/base.py", line 156, in call_next
    raise app_exc
  File "/srv/app/main.py", line 64, in checkout
    result = calculate_total(req.subtotal, req.region)
  File "/srv/app/pricing.py", line 33, in calculate_total
    tax_amount = round(subtotal * tax.rate, 2)  # AttributeError when tax is None
AttributeError: 'NoneType' object has no attribute 'rate'
"""

PRICING = "\n".join(f"line {i}" for i in range(1, 33)) + \
    "\n    tax_amount = round(subtotal * tax.rate, 2)  # AttributeError when tax is None\n" + \
    "\n".join(f"line {i}" for i in range(34, 50))


# ---------- redaction ----------

@pytest.mark.parametrize("raw, leaked", [
    ("Authorization: Bearer abcdef1234567890", "abcdef1234567890"),
    ("password=hunter2secret", "hunter2secret"),
    ('api_key: "sk_live_abcdefghijklmnop1234"', "sk_live_abcdefghijklmnop1234"),
    ("aws AKIAABCDEFGHIJKLMNOP used", "AKIAABCDEFGHIJKLMNOP"),
    ("postgres://nexora:topsecret@db:5432/x", "topsecret"),
    ("user jane.doe@example.com failed", "jane.doe@example.com"),
    ("card 4111 1111 1111 1111 declined", "4111 1111 1111 1111"),
    ("token ghp_abcdefghijklmnopqrstuvwxyz123456", "ghp_abcdefghijklmnopqrstuvwxyz123456"),
])
def test_redaction_removes_secrets(raw, leaked):
    assert leaked not in redact(raw)


def test_redaction_keeps_useful_text():
    text = "AttributeError: 'NoneType' object has no attribute 'rate' at line 33"
    assert redact(text) == text


def test_redact_obj_is_recursive():
    out = redact_obj({"a": ["password=abcdef"], "b": {"c": "x@y.com"}})
    assert "abcdef" not in json.dumps(out) and "x@y.com" not in json.dumps(out)


# ---------- stack traces ----------

def test_parse_frames_uses_final_traceback_and_marks_app_frames():
    frames = parse_frames(STACK, "/srv/")
    assert [f.relative_path for f in frames if f.is_app] == ["app/main.py", "app/main.py", "app/pricing.py"]
    deepest = deepest_app_frame(frames)
    assert (deepest.relative_path, deepest.line, deepest.function) == ("app/pricing.py", 33, "calculate_total")
    assert "tax.rate" in deepest.source_line
    assert exception_line(STACK) == "AttributeError: 'NoneType' object has no attribute 'rate'"


def test_parse_frames_without_prefix_still_finds_app_frames():
    deepest = deepest_app_frame(parse_frames(STACK, None))
    assert deepest.function == "calculate_total" and deepest.relative_path == "srv/app/pricing.py"


# ---------- catalog ----------

def test_seed_catalog_and_versions(session, settings, tmp_path):
    cat = tmp_path / "catalog.yaml"
    cat.write_text("""services:
  - name: shop-demo
    environment: local
    source_root: /repos/shop-demo
    runtime_path_prefix: /srv/
    telemetry_service_name: shop-demo
    baseline_deployment: {version: v1.0.0, commit_sha: baseline, description: first}
""")
    settings.service_catalog_file = str(cat)
    seed_from_file(session, settings)
    seed_from_file(session, settings)  # idempotent
    svc = session.scalars(select(Service)).one()
    assert len(session.scalars(select(ServiceDeployment)).all()) == 1
    assert next_version(session, svc) == "v1.1.0"
    assert record_bench_release(session, settings, True) == "v1.1.0"
    dep = current_deployment(session, svc)
    assert dep.version == "v1.1.0" and dep.changed_files == ["app/pricing.py"]
    assert record_bench_release(session, settings, True, "db_pool_leak", ["app/orders.py"]) == "v1.2.0"
    dep = current_deployment(session, svc)
    assert dep.changed_files == ["app/orders.py"] and "save_order" in dep.description


# ---------- collection end to end (mocked sources) ----------

def make_bench(session, settings, tmp_path, minutes_before=3):
    src = tmp_path / "src"
    (src / "app").mkdir(parents=True)
    (src / "app" / "pricing.py").write_text(PRICING)
    svc = Service(organization_id="org_local", name="shop-demo", environment="local", source_root=str(src),
                  runtime_path_prefix="/srv/", telemetry_service_name="shop-demo")
    session.add(svc)
    session.flush()
    now = datetime.now(timezone.utc)
    session.add(ServiceDeployment(organization_id="org_local", service_id=svc.id, version="v1.0.0",
                                  deployed_at=now - timedelta(hours=2), changed_files=[]))
    session.add(ServiceDeployment(organization_id="org_local", service_id=svc.id, version="v1.1.0",
                                  deployed_at=now - timedelta(minutes=minutes_before),
                                  changed_files=["app/pricing.py"], description="tax refactor"))
    inc = Incident(organization_id="org_local", key="INC-1001", title="5xx", service="shop-demo",
                   environment="local", severity="critical", detected_at=now - timedelta(minutes=1))
    session.add(inc)
    session.commit()
    return inc


def trace_body(ns: str, gateway_seconds: float = 0.02, gateway_error: bool = False) -> dict:
    start = int(ns) - 3_000_000_000
    server = {
        "spanId": "s1", "name": "POST /checkout", "kind": "SPAN_KIND_SERVER",
        "startTimeUnixNano": str(start), "endTimeUnixNano": str(start + int((gateway_seconds + 0.1) * 1e9)),
        "attributes": [{"key": "url.path", "value": {"stringValue": "/checkout"}}],
        "events": [{"name": "exception", "timeUnixNano": ns, "attributes": [
            {"key": "exception.type", "value": {"stringValue": "AttributeError"}},
            {"key": "exception.message", "value": {"stringValue": "'NoneType' object has no attribute 'rate'"}},
            {"key": "exception.stacktrace", "value": {"stringValue": STACK}},
        ]}],
    }
    client = {
        "spanId": "c1", "parentSpanId": "s1", "name": "POST payment-gateway /charge", "kind": "SPAN_KIND_CLIENT",
        "startTimeUnixNano": str(start + 50_000_000),
        "endTimeUnixNano": str(start + 50_000_000 + int(gateway_seconds * 1e9)),
        "attributes": [{"key": "peer.service", "value": {"stringValue": "payment-gateway"}}],
        "status": {"code": "STATUS_CODE_ERROR"} if gateway_error else {},
    }
    return {"trace": {"resourceSpans": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "shop-demo"}}]},
        "scopeSpans": [{"spans": [server, client]}]}]}}


def full_stack_handler(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    ns = str(int(datetime.now(timezone.utc).timestamp() * 1e9))
    if host == "tempo" and path == "/api/search":
        return httpx.Response(200, json={"traces": [{"traceID": "abc"}, {"traceID": "def"}]})
    if host == "tempo" and path.startswith("/api/v2/traces/"):
        assert len(path.rsplit("/", 1)[1]) == 32  # short ids are padded
        return httpx.Response(200, json=trace_body(ns))
    if host == "loki":
        return httpx.Response(200, json={"data": {"result": [{"stream": {
            "exception_type": "AttributeError", "exception_message": "'NoneType' object has no attribute 'rate'",
            "exception_stacktrace": STACK + "\npassword=letmein123"},
            "values": [[ns, "Unhandled error on POST /checkout user bob@example.com"]]}]}})
    if host == "prom":
        t = datetime.now(timezone.utc).timestamp()
        values = [[t - 600 + 15 * i, "0" if i < 20 else "0.4"] for i in range(40)]
        return httpx.Response(200, json={"data": {"result": [{"values": values}]}})
    return httpx.Response(404)


def urls(settings):
    settings.tempo_url, settings.loki_url, settings.prometheus_url = "http://tempo", "http://loki", "http://prom"


def test_collect_evidence_finds_faulty_line(session, settings, tmp_path):
    inc = make_bench(session, settings, tmp_path)
    urls(settings)
    client = httpx.Client(transport=httpx.MockTransport(full_stack_handler))
    result = collect_evidence(session, settings, str(inc.id), http=client)
    assert result["evidence"] >= 6 and result["gaps"] == 0

    items = {e.kind: e for e in session.scalars(select(Evidence)).all()}
    assert set(items) == {"trace", "log", "metric", "dependency", "deployment", "code"}
    assert items["dependency"].content["peer"] == "payment-gateway" and not items["dependency"].content["slow"]
    code = items["code"]
    assert code.content["file"] == "app/pricing.py" and code.content["line"] == 33
    assert code.content["changed_in_deployment"] is True
    assert "tax.rate" in code.content["failing_line"]
    assert any(l["highlight"] and l["n"] == 33 for l in code.content["snippet"])
    assert items["deployment"].content["version"] == "v1.1.0"
    metrics = {e.content["metric"]: e for e in session.scalars(select(Evidence).where(Evidence.kind == "metric")).all()}
    assert metrics["error_ratio"].content["peak"] == 0.4 and metrics["error_ratio"].content["breached"]
    assert not metrics["latency_p95"].content["breached"]  # 0.4 s is under the 1 s default

    everything = json.dumps([e.content for e in items.values()]) + json.dumps([e.summary for e in items.values()])
    assert "letmein123" not in everything and "bob@example.com" not in everything

    session.refresh(inc)
    assert inc.status == "investigating"
    run = session.scalars(select(PipelineRun).where(PipelineRun.trigger_kind == "evidence_collection")).one()
    assert run.status == "success"
    assert [s.stage for s in run.steps] == ["catalog", "window", "traces", "logs", "metrics", "dependencies", "deployments",
                                            "code", "stored"]
    assert session.scalars(select(IncidentEvent).where(IncidentEvent.kind == "evidence_collected")).one()


def test_sources_down_become_gaps_not_crashes(session, settings, tmp_path):
    inc = make_bench(session, settings, tmp_path)
    urls(settings)

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    result = collect_evidence(session, settings, str(inc.id), http=httpx.Client(transport=httpx.MockTransport(down)))
    kinds = [e.kind for e in session.scalars(select(Evidence)).all()]
    assert "deployment" in kinds  # catalog still works without the network
    assert kinds.count("gap") == 5  # traces, logs, metrics, dependencies failed; code has no frames
    run = session.scalars(select(PipelineRun).where(PipelineRun.trigger_kind == "evidence_collection")).one()
    statuses = {s.stage: s.status for s in run.steps}
    assert statuses["traces"] == "failed" and statuses["code"] == "warning"
    assert run.status == "failed"
    assert result["gaps"] == 5


def test_empty_sources_are_warnings(session, settings, tmp_path):
    inc = make_bench(session, settings, tmp_path)
    urls(settings)

    def empty(request):
        if request.url.host == "tempo":
            return httpx.Response(200, json={"traces": []})
        if request.url.host == "loki":
            return httpx.Response(200, json={"data": {"result": []}})
        return httpx.Response(200, json={"data": {"result": []}})

    collect_evidence(session, settings, str(inc.id), http=httpx.Client(transport=httpx.MockTransport(empty)))
    run = session.scalars(select(PipelineRun).where(PipelineRun.trigger_kind == "evidence_collection")).one()
    statuses = {s.stage: s.status for s in run.steps}
    assert statuses["traces"] == statuses["logs"] == statuses["metrics"] == statuses["dependencies"] == "warning"
    assert run.status == "warning"


def test_unknown_service_is_reported(session, settings):
    inc = Incident(organization_id="org_local", key="INC-1009", title="x", service="ghost", environment="local",
                   severity="high")
    session.add(inc)
    session.commit()
    result = collect_evidence(session, settings, str(inc.id), http=httpx.Client())
    assert result == {"evidence": 0, "gaps": 1}
    assert session.scalars(select(Evidence)).one().kind == "gap"


def test_code_collector_never_reads_outside_source_root(session, settings, tmp_path):
    inc = make_bench(session, settings, tmp_path)
    (tmp_path / "secret.txt").write_text("top secret")
    urls(settings)
    evil = STACK.replace("/srv/app/pricing.py", "/srv/../secret.txt")

    def handler(request):
        if request.url.host == "loki":
            return httpx.Response(200, json={"data": {"result": [{"stream": {"exception_stacktrace": evil,
                                                                         "exception_type": "X"},
                                                              "values": [["1", "boom"]]}]}})
        return httpx.Response(200, json={"traces": [], "data": {"result": []}})

    collect_evidence(session, settings, str(inc.id), http=httpx.Client(transport=httpx.MockTransport(handler)))
    dump = json.dumps([e.content for e in session.scalars(select(Evidence)).all()])
    assert "top secret" not in dump


def test_evidence_api_and_recollect(client, factory, settings, tmp_path):
    with factory() as s:
        inc = make_bench(s, settings, tmp_path)
        urls(settings)
        collect_evidence(s, settings, str(inc.id), http=httpx.Client(transport=httpx.MockTransport(full_stack_handler)))
        inc_id = str(inc.id)
    data = client.get(f"/api/v1/incidents/{inc_id}/evidence").json()
    assert data["collection"]["status"] == "success"
    assert data["items"][0]["kind"] == "code"  # most useful first
    r = client.post(f"/api/v1/incidents/{inc_id}/evidence/collect")
    assert r.status_code == 202 and r.json()["status"] == "queued"
    assert client.get(f"/api/v1/incidents/{inc_id}/evidence", headers={"X-Nexora-Organization-Id": "other"}).status_code == 404


def test_caller_only_frames_are_dropped(session, settings, tmp_path):
    inc = make_bench(session, settings, tmp_path)
    (tmp_path / "src" / "app" / "main.py").write_text("\n".join(f"m{i}" for i in range(1, 80)))
    urls(settings)
    caller_only = 'Traceback (most recent call last):\n  File "/srv/app/main.py", line 40, in trace_requests\n    x\nError: y\n'

    def handler(request):
        if request.url.host == "loki":
            return httpx.Response(200, json={"data": {"result": [{"stream": {"exception_stacktrace": caller_only,
                                                                         "exception_type": "Error"},
                                                              "values": [["1", "boom"]]}]}})
        return full_stack_handler(request)

    collect_evidence(session, settings, str(inc.id), http=httpx.Client(transport=httpx.MockTransport(handler)))
    code = [e.content["file"] for e in session.scalars(select(Evidence).where(Evidence.kind == "code")).all()]
    assert code == ["app/pricing.py"]


def test_truncated_trace_stack_is_flagged_not_trusted(session, settings, tmp_path):
    inc = make_bench(session, settings, tmp_path)
    urls(settings)
    cut = STACK[: STACK.index('  File "/srv/app/pricing.py"')]  # cut before the faulty frame

    def handler(request):
        if request.url.host == "tempo" and request.url.path.startswith("/api/v2/traces/"):
            body = full_stack_handler(request).json()
            ev = body["trace"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["events"][0]
            ev["attributes"][2]["value"]["stringValue"] = cut
            return httpx.Response(200, json=body)
        return full_stack_handler(request)

    collect_evidence(session, settings, str(inc.id), http=httpx.Client(transport=httpx.MockTransport(handler)))
    trace = session.scalars(select(Evidence).where(Evidence.kind == "trace")).one()
    assert trace.content["stacktrace_truncated"] is True and "truncated" in trace.summary
    code = session.scalars(select(Evidence).where(Evidence.kind == "code")).all()
    assert [c.content["file"] for c in code] == ["app/pricing.py"]  # found via the complete log stack



def test_metric_specs_use_catalog_over_defaults():
    from app.modules.evidence.collectors import metric_specs

    svc = Service(organization_id="o", name="api", environment="prod", telemetry_service_name="api",
                  config={"metrics": {"latency_p95": {"threshold": 0.3},
                                      "saturation": [{"name": "pool", "title": "Pool", "query": 'x{s="{service}"}'}]}})
    specs = {sp["name"]: sp for sp in metric_specs(svc)}
    assert 'service_name="api"' in specs["error_ratio"]["query"]  # OpenTelemetry default
    assert specs["latency_p95"]["threshold"] == 0.3 and "http_server_request_duration_seconds_bucket" in specs["latency_p95"]["query"]
    assert specs["pool"]["query"] == 'x{s="api"}' and specs["pool"]["threshold"] == 0.9


def test_slow_and_failing_dependency_is_flagged(session, settings, tmp_path):
    inc = make_bench(session, settings, tmp_path)
    urls(settings)

    def handler(request):
        if request.url.host == "tempo" and request.url.path.startswith("/api/v2/traces/"):
            ns = str(int(datetime.now(timezone.utc).timestamp() * 1e9))
            return httpx.Response(200, json=trace_body(ns, gateway_seconds=2.5, gateway_error=True))
        return full_stack_handler(request)

    collect_evidence(session, settings, str(inc.id), http=httpx.Client(transport=httpx.MockTransport(handler)))
    dep = session.scalars(select(Evidence).where(Evidence.kind == "dependency")).one()
    assert dep.content["slow"] and dep.content["failing"] and dep.relevance > 0.8
    assert dep.content["share_of_request"] > 0.9
    assert "slow and failing" in dep.title


def test_saturation_metric_breach(session, settings, tmp_path):
    inc = make_bench(session, settings, tmp_path)
    svc = session.scalars(select(Service)).one()
    svc.config = {"metrics": {"saturation": [{"name": "db_pool", "title": "DB pool use", "query": "pool",
                                              "threshold": 0.9}]}}
    session.commit()
    urls(settings)

    def handler(request):
        if request.url.host == "prom" and request.url.params.get("query") == "pool":
            t = datetime.now(timezone.utc).timestamp()
            return httpx.Response(200, json={"data": {"result": [{"values": [[t - 60, "0.4"], [t, "1"]]}]}})
        return full_stack_handler(request)

    collect_evidence(session, settings, str(inc.id), http=httpx.Client(transport=httpx.MockTransport(handler)))
    pool = [e for e in session.scalars(select(Evidence).where(Evidence.kind == "metric")).all()
            if e.content["metric"] == "db_pool"][0]
    assert pool.content["breached"] and pool.title.startswith("DB pool use peaked at 100%")


def test_window_stops_at_previous_incident_clearing(session, settings, tmp_path):
    from app.modules.evidence.collect import evidence_window_start

    inc = make_bench(session, settings, tmp_path)
    now = datetime.now(timezone.utc)
    old = Incident(organization_id="org_local", key="INC-0999", title="old", service="shop-demo",
                   environment="local", severity="critical", detected_at=now - timedelta(minutes=12))
    session.add(old)
    session.flush()
    cleared = now - timedelta(minutes=6)
    session.add(IncidentEvent(organization_id="org_local", incident_id=old.id, kind="alerts_cleared",
                              message="cleared", occurred_at=cleared))
    session.commit()
    start, note = evidence_window_start(session, inc, settings, now - timedelta(minutes=1))
    assert abs((start - cleared).total_seconds()) < 1 and "INC-0999" in note
    # an unrelated service's incident does not narrow the window
    old.service = "payment-api"
    session.commit()
    start, note = evidence_window_start(session, inc, settings, now - timedelta(minutes=1))
    assert note is None and start < cleared


def test_dependency_collector_inspects_slowest_traces_first(session, settings, tmp_path):
    inc = make_bench(session, settings, tmp_path)
    urls(settings)
    ns = str(int(datetime.now(timezone.utc).timestamp() * 1e9))
    fast_errors = [{"traceID": f"e{i}", "durationMs": 5} for i in range(12)]  # fail before calling anyone

    def handler(request):
        if request.url.host == "tempo" and request.url.path == "/api/search" and "duration" in request.url.params["q"]:
            return httpx.Response(200, json={"traces": fast_errors + [{"traceID": "slow1", "durationMs": 2600}]})
        if request.url.host == "tempo" and request.url.path.startswith("/api/v2/traces/"):
            if request.url.path.endswith("slow1"):
                return httpx.Response(200, json=trace_body(ns, gateway_seconds=2.5))
            return httpx.Response(200, json={"trace": {"resourceSpans": []}})
        return full_stack_handler(request)

    collect_evidence(session, settings, str(inc.id), http=httpx.Client(transport=httpx.MockTransport(handler)))
    dep = session.scalars(select(Evidence).where(Evidence.kind == "dependency")).one()
    assert dep.content["slow"] and dep.content["peer"] == "payment-gateway"


# ---------- machine-local catalog overrides (native Windows run) ----------

CATALOG_WITH_ACTIONS = """services:
  - name: shop-demo
    environment: local
    source_root: /repos/shop-demo
    runtime_path_prefix: /srv/
    language: python
    telemetry_service_name: shop-demo
    actions:
      - {type: rollback_release, title: Roll back, risk: low, approval: single, params: {}, preconditions: []}
    metrics:
      error_ratio: {title: 5xx error ratio, threshold: 0.05}
"""


def _seed(session, settings, tmp_path, overrides: str | None):
    cat = tmp_path / "catalog.yaml"
    cat.write_text(CATALOG_WITH_ACTIONS)
    settings.service_catalog_file = str(cat)
    if overrides is not None:
        (tmp_path / "local.yaml").write_text(overrides)
        settings.service_catalog_overrides_file = str(tmp_path / "local.yaml")
    seed_from_file(session, settings)
    return session.scalars(select(Service)).one()


def test_overrides_change_only_source_root_and_prefix(session, settings, tmp_path):
    svc = _seed(session, settings, tmp_path, """services:
  - name: shop-demo
    environment: local
    source_root: E:/work/shop-demo
    runtime_path_prefix: E:/work/shop-demo/
    telemetry_service_name: hijacked
    actions: []
""")
    assert svc.source_root == "E:/work/shop-demo" and svc.runtime_path_prefix == "E:/work/shop-demo/"
    assert svc.telemetry_service_name == "shop-demo"  # not overridable
    assert svc.config["actions"][0]["type"] == "rollback_release"  # actions and metrics untouched
    assert svc.config["metrics"]["error_ratio"]["threshold"] == 0.05


def test_no_overrides_file_keeps_shared_catalog(session, settings, tmp_path):
    svc = _seed(session, settings, tmp_path, None)
    assert settings.service_catalog_overrides_file == ""
    assert (svc.source_root, svc.runtime_path_prefix) == ("/repos/shop-demo", "/srv/")


def test_missing_or_broken_overrides_file_is_ignored(session, settings, tmp_path):
    cat = tmp_path / "catalog.yaml"
    cat.write_text(CATALOG_WITH_ACTIONS)
    settings.service_catalog_file = str(cat)
    settings.service_catalog_overrides_file = str(tmp_path / "does-not-exist.yaml")
    seed_from_file(session, settings)
    svc = session.scalars(select(Service)).one()
    assert (svc.source_root, svc.runtime_path_prefix) == ("/repos/shop-demo", "/srv/")

    (tmp_path / "broken.yaml").write_text("services: [ {name: shop-demo, source_root: ")
    settings.service_catalog_overrides_file = str(tmp_path / "broken.yaml")
    seed_from_file(session, settings)  # a broken override never breaks seeding
    session.refresh(svc)
    assert (svc.source_root, svc.runtime_path_prefix) == ("/repos/shop-demo", "/srv/")


def test_override_for_other_service_or_environment_is_ignored(session, settings, tmp_path):
    svc = _seed(session, settings, tmp_path, """services:
  - {name: payment-api, environment: local, source_root: E:/other}
  - {name: shop-demo, environment: prod, source_root: E:/prod}
""")
    assert (svc.source_root, svc.runtime_path_prefix) == ("/repos/shop-demo", "/srv/")


# STACK as a native Windows run prints it.
WIN_EVIDENCE_STACK = (STACK.replace("/usr/local/lib/python3.12/site-packages/starlette/middleware/base.py",
                                    "C:\\Python311\\Lib\\site-packages\\starlette\\middleware\\base.py")
                      .replace("/srv/app/main.py", "C:\\svc\\shop\\app\\main.py")
                      .replace("/srv/app/pricing.py", "C:\\svc\\shop\\app\\pricing.py"))


def test_code_evidence_from_windows_stack_traces(session, settings, tmp_path):
    assert "/srv/" not in WIN_EVIDENCE_STACK and "C:\\svc\\shop\\app\\pricing.py\", line 33" in WIN_EVIDENCE_STACK
    inc = make_bench(session, settings, tmp_path)
    svc = session.scalars(select(Service)).one()
    svc.runtime_path_prefix = "c:/svc/shop/"  # forward slashes and lower-case drive still match
    session.commit()
    urls(settings)

    def handler(request):
        host, path = request.url.host, request.url.path
        ns = str(int(datetime.now(timezone.utc).timestamp() * 1e9))
        if host == "tempo" and path.startswith("/api/v2/traces/"):
            body = trace_body(ns)
            event = body["trace"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["events"][0]
            event["attributes"][2]["value"]["stringValue"] = WIN_EVIDENCE_STACK
            return httpx.Response(200, json=body)
        if host == "loki":
            return httpx.Response(200, json={"data": {"result": [{"stream": {
                "exception_type": "AttributeError", "exception_message": "'NoneType' object has no attribute 'rate'",
                "exception_stacktrace": WIN_EVIDENCE_STACK}, "values": [[ns, "Unhandled error on POST /checkout"]]}]}})
        return full_stack_handler(request)

    collect_evidence(session, settings, str(inc.id), http=httpx.Client(transport=httpx.MockTransport(handler)))
    code = session.scalars(select(Evidence).where(Evidence.kind == "code")).one()
    assert code.content["file"] == "app/pricing.py" and code.content["line"] == 33
    assert code.content["changed_in_deployment"] is True
    assert all("\\" not in c["file"] for c in code.content["call_chain"])
    trace = session.scalars(select(Evidence).where(Evidence.kind == "trace")).one()
    assert trace.content["app_frames"][-1]["relative_path"] == "app/pricing.py"


# ---------- Tempo stalls: one unreadable trace must not hide the others ----------

def _steps(session):
    run = session.scalars(select(PipelineRun).where(PipelineRun.trigger_kind == "evidence_collection")).one()
    return {s.stage: s for s in run.steps}


def test_trace_download_timeout_skips_that_trace(session, settings, tmp_path):
    inc = make_bench(session, settings, tmp_path)
    urls(settings)
    downloads = []

    def handler(request):
        if request.url.host == "tempo" and request.url.path.startswith("/api/v2/traces/"):
            downloads.append(request.url.path)
            if request.url.path.endswith("abc"):  # the first search result stalls (Tempo 499 at 5 s)
                raise httpx.ReadTimeout("timed out", request=request)
        return full_stack_handler(request)

    collect_evidence(session, settings, str(inc.id), http=httpx.Client(transport=httpx.MockTransport(handler)))
    trace = session.scalars(select(Evidence).where(Evidence.kind == "trace")).one()
    assert trace.content["exception_type"] == "AttributeError"
    assert trace.content["unreadable_traces"] == [{"trace_id": "abc".rjust(32, "0"), "error": "ReadTimeout"}]
    assert "1 of 2 trace(s) could not be read (ReadTimeout)" in trace.summary
    assert trace.content["error_trace_count"] == 2  # existing fields unchanged
    assert _steps(session)["traces"].status == "success"
    code = session.scalars(select(Evidence).where(Evidence.kind == "code")).one()
    assert code.content["file"] == "app/pricing.py"
    assert sum(p.endswith("abc") for p in downloads) == 2  # once per collector: never re-requested


def test_no_readable_trace_still_fails_the_source(session, settings, tmp_path):
    inc = make_bench(session, settings, tmp_path)
    urls(settings)

    def handler(request):
        if request.url.host == "tempo" and request.url.path.startswith("/api/v2/traces/"):
            raise httpx.ReadTimeout("timed out", request=request)
        return full_stack_handler(request)

    collect_evidence(session, settings, str(inc.id), http=httpx.Client(transport=httpx.MockTransport(handler)))
    steps = _steps(session)
    assert steps["traces"].status == "failed" and "ReadTimeout" in steps["traces"].detail
    assert steps["dependencies"].status == "failed"
    assert not session.scalars(select(Evidence).where(Evidence.kind == "trace")).all()
    assert session.scalars(select(Evidence).where(Evidence.kind == "code")).one()  # logs still give the frame


def test_unreadable_traces_are_bounded(session, settings, tmp_path):
    inc = make_bench(session, settings, tmp_path)
    urls(settings)
    downloads = []

    def handler(request):
        if request.url.host == "tempo" and request.url.path == "/api/search":
            return httpx.Response(200, json={"traces": [{"traceID": f"t{i}", "durationMs": 10} for i in range(20)]})
        if request.url.host == "tempo" and request.url.path.startswith("/api/v2/traces/"):
            downloads.append(request.url.path)
            raise httpx.ReadTimeout("timed out", request=request)
        return full_stack_handler(request)

    collect_evidence(session, settings, str(inc.id), http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(downloads) == (5 + 2) + (10 + 2)  # traces wants 5, dependencies 10, each tries 2 extra at most


def test_dependency_download_failure_skips_that_trace(session, settings, tmp_path):
    inc = make_bench(session, settings, tmp_path)
    urls(settings)
    ns = str(int(datetime.now(timezone.utc).timestamp() * 1e9))

    def handler(request):
        if request.url.host == "tempo" and request.url.path == "/api/search" and "duration" in request.url.params["q"]:
            return httpx.Response(200, json={"traces": [{"traceID": "stall", "durationMs": 2700},
                                                        {"traceID": "slow1", "durationMs": 2600}]})
        if request.url.host == "tempo" and request.url.path.endswith("stall"):
            return httpx.Response(500, text="rpc error")
        if request.url.host == "tempo" and request.url.path.endswith("slow1"):
            return httpx.Response(200, json=trace_body(ns, gateway_seconds=2.5))
        return full_stack_handler(request)

    collect_evidence(session, settings, str(inc.id), http=httpx.Client(transport=httpx.MockTransport(handler)))
    dep = session.scalars(select(Evidence).where(Evidence.kind == "dependency")).one()
    assert dep.content["slow"] and dep.content["peer"] == "payment-gateway"
    assert dep.content["unreadable_traces"] == [{"trace_id": "stall".rjust(32, "0"), "error": "HTTPStatusError"}]
    assert "1 of 2 trace(s) could not be read (HTTPStatusError)" in dep.summary


def test_failing_requests_without_outbound_calls_keep_the_same_gap(session, settings, tmp_path):
    # INC-1001: code_null_tax fails before charge(), so failing traces only have the SERVER span.
    inc = make_bench(session, settings, tmp_path)
    urls(settings)
    ns = str(int(datetime.now(timezone.utc).timestamp() * 1e9))

    def handler(request):
        if request.url.host == "tempo" and request.url.path.startswith("/api/v2/traces/"):
            body = trace_body(ns)
            spans = body["trace"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
            del spans[1]  # no CLIENT span
            return httpx.Response(200, json=body)
        return full_stack_handler(request)

    collect_evidence(session, settings, str(inc.id), http=httpx.Client(transport=httpx.MockTransport(handler)))
    step = _steps(session)["dependencies"]
    assert step.status == "warning"
    assert step.detail == "gap: 2 slow or failing request(s), but none made traced outbound calls"
