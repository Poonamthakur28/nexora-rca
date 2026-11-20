"""Calls the external payment gateway. The outbound call is traced as a CLIENT span."""

import os

import httpx
from opentelemetry import trace
from opentelemetry.propagate import inject
from opentelemetry.trace import Status, StatusCode

from .bugs import is_enabled

GATEWAY_URL = os.getenv("PAYMENT_GATEWAY_URL", "http://localhost:8090")
tracer = trace.get_tracer("shop-demo.payments")
_client = httpx.Client(timeout=10)


class PaymentGatewayError(Exception):
    pass


def charge(amount: float) -> str:
    headers: dict[str, str] = {}
    params = {}
    if is_enabled("slow_payment_gateway"):
        # Test bench only: tells the gateway to behave like an external system having a bad day.
        params["simulate_delay"] = "2.5"
    with tracer.start_as_current_span("POST payment-gateway /charge", kind=trace.SpanKind.CLIENT) as span:
        span.set_attribute("peer.service", "payment-gateway")
        span.set_attribute("server.address", "payment-gateway")
        span.set_attribute("http.request.method", "POST")
        inject(headers)  # propagate the trace to the gateway
        try:
            resp = _client.post(f"{GATEWAY_URL}/charge", json={"amount": amount}, params=params, headers=headers)
        except httpx.HTTPError as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise PaymentGatewayError(f"payment gateway unreachable: {exc}") from exc
        span.set_attribute("http.response.status_code", resp.status_code)
        if resp.status_code >= 500:
            span.set_status(Status(StatusCode.ERROR))
            raise PaymentGatewayError(f"payment gateway returned HTTP {resp.status_code}")
        return resp.json()["payment_id"]
