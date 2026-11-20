"""shop-demo: a small checkout service used as Nexora's test bench."""

import asyncio
import logging
import os
import random
import time

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from opentelemetry import metrics, trace
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel

from .bugs import BUGS, set_enabled
from .inventory import reserve_stock
from .orders import POOL, save_order
from .payments import charge
from .pricing import calculate_total
from .telemetry import setup_telemetry

setup_telemetry()

app = FastAPI(title="shop-demo")
log = logging.getLogger("shop")
tracer = trace.get_tracer("shop-demo")
meter = metrics.get_meter("shop-demo")

# Step 1 counter (kept for the System Checks page).
requests_counter = meter.create_counter("shop.http.requests", description="HTTP requests by route and status code")
# Standard OpenTelemetry HTTP server metric; Nexora's default queries use this one.
request_duration = meter.create_histogram(
    "http.server.request.duration", unit="s", description="Duration of HTTP server requests"
)


def _pool_in_use(_: CallbackOptions):
    yield Observation(POOL.in_use)


def _pool_size(_: CallbackOptions):
    yield Observation(POOL.size)


meter.create_observable_gauge("shop.db.pool.in_use", [_pool_in_use], description="Database connections in use")
meter.create_observable_gauge("shop.db.pool.size", [_pool_size], description="Database connection pool size")

STARTED = time.time()
SELF_URL = os.getenv("SHOP_DEMO_SELF_URL", "http://localhost:8080")
ADMIN_PREFIXES = ("/admin", "/health", "/metrics")


@app.middleware("http")
async def trace_requests(request: Request, call_next):
    route = request.url.path
    if route.startswith(ADMIN_PREFIXES):
        return await call_next(request)  # keep test-bench controls out of the service's telemetry
    start = time.perf_counter()
    with tracer.start_as_current_span(f"{request.method} {route}", kind=trace.SpanKind.SERVER) as span:
        span.set_attribute("http.request.method", request.method)
        span.set_attribute("http.route", route)
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception as exc:  # unhandled error -> 500, recorded on the span
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            log.exception("Unhandled error on %s %s: %s", request.method, route, exc)
            response = JSONResponse({"error": "internal server error"}, status_code=500)
        span.set_attribute("http.response.status_code", status)
        if status >= 500:
            span.set_status(Status(StatusCode.ERROR))
        attrs = {"http.request.method": request.method, "http.route": route, "http.response.status_code": status}
        request_duration.record(time.perf_counter() - start, attrs)
        requests_counter.add(1, {"route": route, "status_code": str(status)})
        return response


class CheckoutRequest(BaseModel):
    subtotal: float
    region: str
    sku: str = "SKU-1"


@app.post("/checkout")
def checkout(req: CheckoutRequest):
    result = calculate_total(req.subtotal, req.region)
    if not reserve_stock(req.sku):
        raise HTTPException(409, f"{req.sku} is out of stock")
    payment_id = charge(result["total"])
    order_id = save_order({**result, "sku": req.sku, "payment_id": payment_id})
    log.info("Checkout ok region=%s sku=%s total=%s order=%s", req.region, req.sku, result["total"], order_id)
    return {**result, "order_id": order_id}


@app.get("/health")
def health():
    return {"status": "ok", "service": "shop-demo", "version": os.getenv("APP_VERSION", "v1.0.0")}


@app.get("/metrics", response_class=PlainTextResponse)
def liveness_metrics():
    # Minimal Prometheus endpoint so Prometheus can tell whether shop-demo is up.
    return f"shop_demo_uptime_seconds {time.time() - STARTED:.0f}\n"


@app.get("/admin/bugs")
def list_bugs():
    return BUGS


@app.post("/admin/bugs/{name}/{action}")
def toggle_bug(name: str, action: str):
    if action not in ("enable", "disable"):
        raise HTTPException(400, "action must be enable or disable")
    try:
        result = set_enabled(name, action == "enable")
    except KeyError:
        raise HTTPException(404, f"unknown bug {name}")
    if name == "db_pool_leak" and action == "disable":
        POOL.reset()  # like restarting the service: leaked connections are returned
    log.warning("Planted bug %s %sd", name, action)
    return result


@app.post("/admin/restart")
def restart_service():
    """Simulate a service restart by returning all leaked database connections."""
    before = {"pool_in_use": POOL.in_use, "pool_size": POOL.size}
    POOL.reset()
    log.warning("Service connection pool restarted")
    return {"restarted": True, "before": before, "after": {"pool_in_use": POOL.in_use, "pool_size": POOL.size}}


def _random_order() -> dict:
    return {
        "subtotal": round(random.uniform(100, 5000), 2),
        "region": random.choice(["KA", "MH", "DL", "TN", "GJ"]),  # TN and GJ have no explicit tax rule
        "sku": random.choice(["SKU-1", "SKU-2", "SKU-3", "SKU-404"]),  # SKU-404 is not stocked
    }


_load_task: asyncio.Task | None = None
_load_state: dict = {"running": False, "rps": 0, "ends_at": None, "sent": 0}


async def _run_load(rps: int, seconds: int) -> None:
    _load_state.update(running=True, rps=rps, ends_at=time.time() + seconds, sent=0)

    async def one(client: httpx.AsyncClient) -> None:
        try:
            await client.post("/checkout", json=_random_order())
        except httpx.HTTPError:
            pass
        _load_state["sent"] += 1

    try:
        async with httpx.AsyncClient(base_url=SELF_URL, timeout=15) as client:
            pending: set[asyncio.Task] = set()
            while time.time() < _load_state["ends_at"]:
                # Requests run concurrently, like real users: a slow dependency does not slow the senders.
                for _ in range(rps):
                    t = asyncio.create_task(one(client))
                    pending.add(t)
                    t.add_done_callback(pending.discard)
                await asyncio.sleep(1)
            if pending:
                await asyncio.wait(pending, timeout=15)
    finally:
        _load_state["running"] = False


@app.post("/admin/load/start")
async def start_load(rps: int = 3, seconds: int = 180):
    """Steady background traffic, like real users, so alert rules with `for: 1m` can fire."""
    global _load_task
    if _load_task and not _load_task.done():
        _load_task.cancel()
    _load_task = asyncio.create_task(_run_load(max(1, min(rps, 20)), max(10, min(seconds, 1800))))
    await asyncio.sleep(0)
    return {"started": True, "rps": rps, "seconds": seconds}


@app.post("/admin/load/stop")
async def stop_load():
    if _load_task and not _load_task.done():
        _load_task.cancel()
    _load_state["running"] = False
    return {"stopped": True}


@app.get("/admin/load")
def load_status():
    return _load_state


@app.post("/admin/traffic")
async def generate_traffic(requests: int = 50):
    requests = max(1, min(requests, 500))
    counts: dict[str, int] = {}
    async with httpx.AsyncClient(base_url=SELF_URL, timeout=15) as client:
        for _ in range(requests):
            resp = await client.post("/checkout", json=_random_order())
            key = str(resp.status_code)
            counts[key] = counts.get(key, 0) + 1
    return {"requests": requests, "status_counts": counts}
