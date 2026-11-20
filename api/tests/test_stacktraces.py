"""Stack trace parsing for Python, Java, Node.js and Go, and mapping frames to source files."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import select

from app.db.models import Evidence, Incident, Service, ServiceDeployment
from app.modules.evidence.collect import collect_evidence
from app.modules.evidence.stacktrace import (
    deepest_app_frame,
    detect_language,
    exception_line,
    parse_frames,
    resolve_source_path,
)

JAVA = """java.lang.IllegalStateException: Order could not be priced
\tat com.shop.checkout.CheckoutController.checkout(CheckoutController.java:58)
\tat org.springframework.web.servlet.FrameworkServlet.service(FrameworkServlet.java:897)
Caused by: java.lang.NullPointerException: Cannot invoke "com.shop.pricing.TaxRule.getRate()" because "rule" is null
\tat com.shop.pricing.TaxService.calculateTotal(TaxService.java:42)
\tat com.shop.checkout.CheckoutService.price(CheckoutService.java:31)
\tat com.shop.checkout.CheckoutController.checkout(CheckoutController.java:55)
\tat java.base/jdk.internal.reflect.DirectMethodHandleAccessor.invoke(DirectMethodHandleAccessor.java:103)
\t... 48 more
"""

NODE = """TypeError: Cannot read properties of undefined (reading 'rate')
    at calculateTotal (/app/src/pricing.js:32:28)
    at async checkout (/app/src/routes/checkout.js:14:19)
    at Layer.handle [as handle_request] (/app/node_modules/express/lib/router/layer.js:95:5)
    at process.processTicksAndRejections (node:internal/process/task_queues:95:5)
"""

GO = """panic: runtime error: invalid memory address or nil pointer dereference
[signal SIGSEGV: segmentation violation code=0x1 addr=0x8 pc=0x4a1b2c]

goroutine 34 [running]:
github.com/shop/checkout/pricing.CalculateTotal(0x0, 0x40590000)
\t/app/pricing/pricing.go:32 +0x1d
github.com/shop/checkout/api.(*Handler).Checkout(0xc000120000, {0x6b7e20, 0xc0001a8000})
\t/app/api/handler.go:61 +0x8f
net/http.HandlerFunc.ServeHTTP(0xc000116000, {0x6b7e20, 0xc0001a8000}, 0xc000192000)
\t/usr/local/go/src/net/http/server.go:2136 +0x29

goroutine 1 [IO wait]:
main.main()
\t/app/main.go:20 +0x55
"""


def test_detect_language():
    assert detect_language(JAVA) == "java"
    assert detect_language(NODE) == "node"
    assert detect_language(GO) == "go"
    assert detect_language('Traceback (most recent call last):\n  File "/srv/a.py", line 1, in f\n') == "python"
    assert detect_language("just a message") is None


def test_java_uses_root_cause_and_app_namespaces():
    frames = parse_frames(JAVA, None, app_namespaces=["com.shop"])
    deepest = deepest_app_frame(frames)
    assert deepest.function == "com.shop.pricing.TaxService.calculateTotal" and deepest.line == 42
    assert deepest.relative_path == "com/shop/pricing/TaxService.java"
    assert [f.function.rsplit(".", 1)[1] for f in frames if f.is_app] == ["checkout", "price", "calculateTotal"]
    assert exception_line(JAVA).startswith("java.lang.NullPointerException")


def test_java_without_namespaces_skips_framework_frames():
    deepest = deepest_app_frame(parse_frames(JAVA, None))
    assert deepest.function.startswith("com.shop.pricing")


def test_node_frames_skip_node_modules_and_internals():
    frames = parse_frames(NODE, "/app/")
    deepest = deepest_app_frame(frames)
    assert (deepest.relative_path, deepest.line, deepest.function) == ("src/pricing.js", 32, "calculateTotal")
    assert [f.relative_path for f in frames if f.is_app] == ["src/routes/checkout.js", "src/pricing.js"]
    assert exception_line(NODE).startswith("TypeError")


def test_go_frames_use_panicking_goroutine_only():
    frames = parse_frames(GO, "/app/")
    assert all(f.path != "/app/main.go" for f in frames)  # other goroutines are ignored
    deepest = deepest_app_frame(frames)
    assert (deepest.relative_path, deepest.line) == ("pricing/pricing.go", 32)
    assert deepest.function == "github.com/shop/checkout/pricing.CalculateTotal"
    assert exception_line(GO).startswith("panic: runtime error")


def _tree(root: Path, files: list[str]) -> None:
    for f in files:
        p = root / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(f"line {i}" for i in range(1, 80)))


def test_resolve_source_path_by_suffix(tmp_path):
    _tree(tmp_path, ["src/main/java/com/shop/pricing/TaxService.java", "pricing/pricing.go", "src/pricing.js"])
    assert resolve_source_path(tmp_path, "com/shop/pricing/TaxService.java").name == "TaxService.java"
    assert resolve_source_path(tmp_path, "app/pricing/pricing.go") == (tmp_path / "pricing/pricing.go").resolve()
    assert resolve_source_path(tmp_path, "src/pricing.js") == (tmp_path / "src/pricing.js").resolve()


def test_resolve_source_path_refuses_ambiguous_and_outside(tmp_path):
    _tree(tmp_path, ["a/util.py", "b/util.py"])
    assert resolve_source_path(tmp_path, "util.py") is None  # two equally good matches: do not guess
    (tmp_path.parent / "secret.py").write_text("x")
    assert resolve_source_path(tmp_path, "../secret.py") is None


def test_java_service_end_to_end(session, settings, tmp_path):
    root = tmp_path / "repo"
    _tree(root, ["src/main/java/com/shop/pricing/TaxService.java", "src/main/java/com/shop/checkout/CheckoutService.java"])
    svc = Service(organization_id="org_local", name="checkout-java", environment="prod", source_root=str(root),
                  runtime_path_prefix=None, telemetry_service_name="checkout-java",
                  config={"language": "java", "app_namespaces": ["com.shop"]})
    session.add(svc)
    session.flush()
    session.add(ServiceDeployment(organization_id="org_local", service_id=svc.id, version="2.4.1",
                                  deployed_at=datetime.now(timezone.utc) - timedelta(hours=1),
                                  changed_files=["src/main/java/com/shop/pricing/TaxService.java"]))
    inc = Incident(organization_id="org_local", key="INC-2001", title="500s", service="checkout-java",
                   environment="prod", severity="critical")
    session.add(inc)
    session.commit()
    settings.tempo_url, settings.loki_url, settings.prometheus_url = "http://tempo", "http://loki", "http://prom"

    def handler(request):
        if request.url.host == "loki":
            return httpx.Response(200, json={"data": {"result": [{"stream": {
                "exception_type": "java.lang.NullPointerException", "exception_stacktrace": JAVA},
                "values": [["1", "Request failed"]]}]}})
        return httpx.Response(200, json={"traces": [], "data": {"result": []}})

    collect_evidence(session, settings, str(inc.id), http=httpx.Client(transport=httpx.MockTransport(handler)))
    code = session.scalars(select(Evidence).where(Evidence.kind == "code")).one()
    assert code.content["file"] == "src/main/java/com/shop/pricing/TaxService.java"
    assert code.content["line"] == 42 and code.content["changed_in_deployment"] is True
    assert code.content["failing_line"] == "line 42"  # read from the file: JVM stacks carry no source text


# ---------- native Windows paths ----------

# A Python traceback from shop-demo running natively on Windows (paths as Python prints them).
WIN_STACK = r"""Traceback (most recent call last):
  File "E:\Downloads\nexora-rca\nexora-rca\shop-demo\app\main.py", line 66, in trace_requests
    response = await call_next(request)
  File "C:\Users\dev\AppData\Local\Programs\Python\Python311\Lib\site-packages\starlette\middleware\base.py", line 168, in call_next
    raise app_exc from app_exc.__cause__ or app_exc.__context__
  File "C:\Users\dev\AppData\Local\Programs\Python\Python311\Lib\site-packages\anyio\_backends\_asyncio.py", line 1002, in run
    result = context.run(func, *args)
  File "E:\Downloads\nexora-rca\nexora-rca\shop-demo\app\main.py", line 90, in checkout
    result = calculate_total(req.subtotal, req.region)
  File "E:\Downloads\nexora-rca\nexora-rca\shop-demo\app\pricing.py", line 32, in calculate_total
    tax_amount = round(subtotal * tax.rate, 2)  # AttributeError when tax is None
AttributeError: 'NoneType' object has no attribute 'rate'
"""
WIN_PREFIX = "E:\\Downloads\\nexora-rca\\nexora-rca\\shop-demo\\"


def test_windows_frames_do_not_match_a_container_prefix():
    # The INC-1001 failure: a /srv/ prefix matches none of the Windows frames.
    assert not any(f.is_app for f in parse_frames(WIN_STACK, "/srv/", "python"))


def test_windows_frames_map_with_windows_prefix():
    frames = parse_frames(WIN_STACK, WIN_PREFIX, "python")
    assert [(f.relative_path, f.line) for f in frames if f.is_app] == [
        ("app/main.py", 66), ("app/main.py", 90), ("app/pricing.py", 32)]
    deepest = deepest_app_frame(frames)
    assert (deepest.relative_path, deepest.line, deepest.function) == ("app/pricing.py", 32, "calculate_total")
    assert "tax.rate" in deepest.source_line
    assert frames[0].path.startswith("E:\\")  # the original path is kept as printed


def test_windows_prefix_ignores_separator_style_and_drive_case():
    for prefix in ("e:/downloads/nexora-rca/nexora-rca/shop-demo/", "E:/Downloads/nexora-rca/nexora-rca/shop-demo/",
                   "e:\\Downloads\\NEXORA-RCA\\nexora-rca\\shop-demo\\"):
        deepest = deepest_app_frame(parse_frames(WIN_STACK, prefix, "python"))
        assert deepest is not None and deepest.relative_path == "app/pricing.py", prefix


def test_windows_library_frames_stay_non_app():
    frames = parse_frames(WIN_STACK, WIN_PREFIX, "python")
    assert all(not f.is_app for f in frames if "site-packages" in f.path)


def test_relative_paths_are_always_forward_slashes():
    for prefix in (WIN_PREFIX, None):
        assert all("\\" not in f.relative_path for f in parse_frames(WIN_STACK, prefix, "python") if f.is_app)


def test_posix_prefix_stays_case_sensitive():
    stack = 'Traceback (most recent call last):\n  File "/srv/app/pricing.py", line 3, in f\n    x\nValueError: v\n'
    assert deepest_app_frame(parse_frames(stack, "/srv/")).relative_path == "app/pricing.py"
    assert deepest_app_frame(parse_frames(stack, "/SRV/")) is None


def test_windows_frame_resolves_in_source_tree(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "pricing.py").write_text("x = 1\n")
    deepest = deepest_app_frame(parse_frames(WIN_STACK, WIN_PREFIX, "python"))
    found = resolve_source_path(tmp_path, deepest.relative_path)
    assert found == (tmp_path / "app" / "pricing.py").resolve()
