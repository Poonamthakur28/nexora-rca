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
from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel

from .bugs import BUGS, set_enabled
from .pricing import calculate_total
from .telemetry import setup_telemetry

setup_telemetry()

app = FastAPI(title="shop-demo")
log = logging.getLogger("shop")
tracer = trace.get_tracer("shop-demo")
meter = metrics.get_meter("shop-demo")
requests_counter = meter.create_counter(
    "shop.http.requests", description="HTTP requests by route and status code"
)
STARTED = time.time()
SELF_URL = os.getenv("SHOP_DEMO_SELF_URL", "http://localhost:8080")


@app.middleware("http")
async def trace_requests(request: Request, call_next):
    route = request.url.path
    with tracer.start_as_current_span(f"{request.method} {route}", kind=trace.SpanKind.SERVER) as span:
        span.set_attribute("http.request.method", request.method)
        span.set_attribute("url.path", route)
        try:
            response = await call_next(request)
        except Exception as exc:  # unhandled error -> 500, recorded on the span
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            span.set_attribute("http.response.status_code", 500)
            log.exception("Unhandled error on %s %s: %s", request.method, route, exc)
            requests_counter.add(1, {"route": route, "status_code": "500"})
            return JSONResponse({"error": "internal server error"}, status_code=500)
        span.set_attribute("http.response.status_code", response.status_code)
        if response.status_code >= 500:
            span.set_status(Status(StatusCode.ERROR))
        requests_counter.add(1, {"route": route, "status_code": str(response.status_code)})
        return response


class CheckoutRequest(BaseModel):
    subtotal: float
    region: str


@app.post("/checkout")
def checkout(req: CheckoutRequest):
    result = calculate_total(req.subtotal, req.region)
    log.info("Checkout ok region=%s total=%s", req.region, result["total"])
    return result


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
    log.warning("Planted bug %s %sd", name, action)
    return result


_load_task: asyncio.Task | None = None
_load_state: dict = {"running": False, "rps": 0, "ends_at": None, "sent": 0}


async def _run_load(rps: int, seconds: int) -> None:
    regions = ["KA", "MH", "DL", "TN", "GJ"]
    _load_state.update(running=True, rps=rps, ends_at=time.time() + seconds, sent=0)
    try:
        async with httpx.AsyncClient(base_url=SELF_URL, timeout=5) as client:
            while time.time() < _load_state["ends_at"]:
                for _ in range(rps):
                    body = {"subtotal": round(random.uniform(100, 5000), 2), "region": random.choice(regions)}
                    try:
                        await client.post("/checkout", json=body)
                    except httpx.HTTPError:
                        pass
                    _load_state["sent"] += 1
                await asyncio.sleep(1)
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
    regions = ["KA", "MH", "DL", "TN", "GJ"]  # TN and GJ have no explicit tax rule
    counts: dict[str, int] = {}
    async with httpx.AsyncClient(base_url=SELF_URL, timeout=5) as client:
        for _ in range(requests):
            body = {"subtotal": round(random.uniform(100, 5000), 2), "region": random.choice(regions)}
            resp = await client.post("/checkout", json=body)
            key = str(resp.status_code)
            counts[key] = counts.get(key, 0) + 1
    return {"requests": requests, "status_counts": counts}
