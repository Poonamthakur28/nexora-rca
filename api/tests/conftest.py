import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings, get_settings
from app.db import models  # noqa: F401
from app.db.base import Base, set_session_factory

TOKEN = "test-webhook-token"


@pytest.fixture
def settings() -> Settings:
    closed = "http://127.0.0.1:9"  # nothing listens here: collectors fail fast, tests never need a stack
    return Settings(_env_file=None, alertmanager_webhook_token=TOKEN, database_url="sqlite://",
                    app_env="development", tempo_url=closed, loki_url=closed, prometheus_url=closed,
                    alertmanager_url=closed, shop_demo_url=closed, otel_collector_health_url=closed,
                    service_catalog_file="/nonexistent.yaml", check_timeout_seconds=0.5)


@pytest.fixture
def factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    f = sessionmaker(bind=engine, expire_on_commit=False)
    set_session_factory(f)
    yield f
    engine.dispose()


@pytest.fixture
def session(factory):
    with factory() as s:
        yield s


@pytest.fixture
def client(factory, settings):
    from app.main import create_app
    from app.modules.alert_ingestion.seed import ensure_local_integration

    with factory() as s:
        ensure_local_integration(s, settings)
    app = create_app(seed=False)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        yield c


def am_payload(status="firing", fingerprint="abc123", alertname="ShopDemoHighErrorRate", severity="critical",
               service="shop-demo", environment="local", starts=None,
               ends="0001-01-01T00:00:00Z"):
    from datetime import datetime, timedelta, timezone

    if starts is None:  # relative to now, so tests never depend on the calendar
        starts = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "version": "4",
        "groupKey": '{}:{alertname="%s"}' % alertname,
        "receiver": "nexora",
        "status": status,
        "alerts": [
            {
                "status": status,
                "labels": {"alertname": alertname, "severity": severity, "service": service,
                           "environment": environment},
                "annotations": {"summary": "shop-demo 5xx error rate above 5%", "description": "desc"},
                "startsAt": starts,
                "endsAt": ends,
                "generatorURL": "http://prometheus:9090/graph",
                "fingerprint": fingerprint,
            }
        ],
        "groupLabels": {"alertname": alertname},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
    }


def drain(factory, settings, limit: int = 20) -> int:
    """Run queued jobs until the queue is empty (alert processing, then evidence collection)."""
    from app.worker import run_once

    n = 0
    while n < limit and run_once(factory, settings):
        n += 1
    return n
