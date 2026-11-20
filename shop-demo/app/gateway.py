"""payment-gateway: a stand-in for an external payment provider, as its own service with its own traces."""

import asyncio
import random
import uuid

from fastapi import FastAPI, Request
from opentelemetry import trace
from opentelemetry.propagate import extract

from .telemetry import setup_telemetry

setup_telemetry()
app = FastAPI(title="payment-gateway")
tracer = trace.get_tracer("payment-gateway")


@app.post("/charge")
async def charge(request: Request, simulate_delay: float = 0.0):
    ctx = extract(dict(request.headers))
    with tracer.start_as_current_span("POST /charge", context=ctx, kind=trace.SpanKind.SERVER) as span:
        delay = min(simulate_delay, 10.0) if simulate_delay > 0 else random.uniform(0.01, 0.03)
        span.set_attribute("gateway.processing_seconds", round(delay, 3))
        await asyncio.sleep(delay)
        return {"payment_id": str(uuid.uuid4()), "status": "captured"}


@app.get("/health")
def health():
    return {"status": "ok", "service": "payment-gateway"}
