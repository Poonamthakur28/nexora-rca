import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.db.models import Alert, AlertDelivery, Incident, IncidentEvent, Job, PipelineRun
from app.jobs.queue import claim_next, enqueue, mark_failed, recover_expired_leases
from app.modules.alert_ingestion.alertmanager import AMAlert, normalize
from app.modules.alert_ingestion.fingerprint import alert_fingerprint, delivery_id
from app.modules.alert_ingestion.mapping import normalize_severity, normalize_status
from app.worker import run_once

from .conftest import TOKEN, am_payload, drain

URL = "/api/v1/integrations/alerts/alertmanager"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def post(client, body, headers=AUTH):
    return client.post(URL, content=json.dumps(body), headers={**headers, "Content-Type": "application/json"})


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


# ---------- mapping and identities ----------

def test_severity_and_status_mapping():
    p = "prometheus_alertmanager"
    assert normalize_severity(p, "critical") == "critical"
    assert normalize_severity(p, "Warning ") == "medium"
    assert normalize_severity(p, "bogus") == "unknown"
    assert normalize_severity(p, None) == "unknown"
    assert normalize_status(p, "firing") == "firing"
    assert normalize_status(p, "resolved") == "resolved"
    assert normalize_status(p, "") == "unknown"


def test_fingerprint_is_deterministic_and_scoped():
    a = alert_fingerprint("org1", "am", "int1", "fp", "svc", "Local")
    assert a == alert_fingerprint("org1", "am", "int1", "fp", "svc", "local")
    assert a != alert_fingerprint("org2", "am", "int1", "fp", "svc", "local")
    assert a != alert_fingerprint("org1", "am", "int1", "fp", "other", "local")


def test_delivery_id_ignores_key_order():
    assert delivery_id({"a": 1, "b": [1, 2]}) == delivery_id({"b": [1, 2], "a": 1})


def test_normalize_alert():
    am = AMAlert(**am_payload()["alerts"][0])
    ca = normalize(am, datetime.now(timezone.utc))
    assert ca.title == "shop-demo 5xx error rate above 5%"
    assert ca.service == "shop-demo" and ca.environment == "local"
    assert ca.normalized_severity == "critical" and ca.normalized_status == "firing"
    assert ca.resolved_at is None
    assert (datetime.now(timezone.utc) - ca.started_at).total_seconds() < 300


def test_secret_like_labels_are_scrubbed():
    body = am_payload()
    body["alerts"][0]["labels"]["api_key"] = "leak-me"
    ca = normalize(AMAlert(**body["alerts"][0]), datetime.now(timezone.utc))
    assert "leak-me" not in json.dumps(ca.attributes)


# ---------- gateway ----------

def test_rejects_missing_or_wrong_token(client, session):
    assert post(client, am_payload(), headers={}).status_code == 401
    assert post(client, am_payload(), headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert count(session, AlertDelivery) == 0
    runs = session.scalars(select(PipelineRun)).all()
    assert len(runs) == 2 and all(r.status == "failed" for r in runs)


def test_rejects_invalid_payload(client, session):
    r = client.post(URL, content=b"not json", headers=AUTH)
    assert r.status_code == 422
    r = post(client, {"status": "firing", "alerts": []})
    assert r.status_code == 422
    assert count(session, AlertDelivery) == 0


def test_rejects_oversized_body(client, settings):
    settings.webhook_max_body_bytes = 100
    assert post(client, am_payload()).status_code == 413


def test_accepts_stores_and_queues_atomically(client, session):
    r = post(client, am_payload())
    assert r.status_code == 202
    assert r.json()["status"] == "accepted"
    assert count(session, AlertDelivery) == 1
    job = session.scalars(select(Job)).one()
    assert job.type == "alert.process" and job.status == "queued"
    run = session.scalars(select(PipelineRun)).one()
    assert [s.stage for s in run.steps] == ["received", "authenticated", "validated", "stored", "queued"]
    assert all(s.status == "success" for s in run.steps)
    # Accepted is not "incident created": nothing exists until the worker runs.
    assert count(session, Incident) == 0


def test_duplicate_delivery_is_acknowledged_without_new_data(client, session):
    body = am_payload()  # the same body twice = a retried delivery
    assert post(client, body).status_code == 202
    r = post(client, body)
    assert r.status_code == 200 and r.json()["status"] == "duplicate"
    assert count(session, AlertDelivery) == 1
    assert count(session, Job) == 1


def test_organization_never_taken_from_payload(client, session, factory, settings):
    body = am_payload()
    body["alerts"][0]["labels"]["organization_id"] = "org_attacker"
    post(client, body)
    drain(factory, settings)
    assert {a.organization_id for a in session.scalars(select(Alert)).all()} == {"org_local"}


# ---------- worker processing ----------

def test_firing_alert_creates_incident(client, session, factory, settings):
    post(client, am_payload())
    assert run_once(factory, settings) is True
    alert = session.scalars(select(Alert)).one()
    incident = session.scalars(select(Incident)).one()
    assert alert.normalized_status == "firing"
    assert incident.key == "INC-1001" and incident.status == "open"
    assert incident.service == "shop-demo" and incident.severity == "critical"
    assert incident.active_alerts == 1
    run = session.scalars(select(PipelineRun).where(PipelineRun.trigger_kind == "alert_delivery")).one()
    session.refresh(run)
    assert run.status == "success" and run.incident_id == incident.id
    assert [s.stage for s in run.steps][-3:] == ["normalized", "alerts_stored", "incident_linked"]
    assert session.scalars(select(AlertDelivery)).one().processing_status == "processed"
    # A new incident queues evidence collection right away.
    assert session.scalars(select(Job).where(Job.type == "evidence.collect")).one().status == "queued"


def test_repeat_notification_updates_same_alert_and_incident(client, session, factory, settings):
    post(client, am_payload())
    drain(factory, settings)
    body = am_payload()
    body["alerts"][0]["annotations"]["description"] = "still failing"  # new body, same alert
    assert post(client, body).status_code == 202
    drain(factory, settings)
    assert count(session, Alert) == 1
    assert count(session, Incident) == 1
    assert session.scalars(select(Alert)).one().version == 2


def test_second_alert_same_service_links_to_open_incident(client, session, factory, settings):
    post(client, am_payload())
    drain(factory, settings)
    post(client, am_payload(fingerprint="def456", alertname="ShopDemoDown"))
    drain(factory, settings)
    assert count(session, Alert) == 2
    incident = session.scalars(select(Incident)).one()
    assert incident.active_alerts == 2


def test_different_service_gets_its_own_incident(client, session, factory, settings):
    post(client, am_payload())
    drain(factory, settings)
    post(client, am_payload(fingerprint="zzz", service="payment-api"))
    drain(factory, settings)
    assert count(session, Incident) == 2


def test_resolved_alert_keeps_incident_open_until_verified(client, session, factory, settings):
    post(client, am_payload())
    drain(factory, settings)
    post(client, am_payload(status="resolved", ends="2026-11-17T18:10:00Z"))
    drain(factory, settings)
    alert = session.scalars(select(Alert)).one()
    incident = session.scalars(select(Incident)).one()
    session.refresh(incident)
    assert alert.normalized_status == "resolved" and alert.resolved_at is not None
    assert incident.status == "open" and incident.active_alerts == 0
    kinds = [e.kind for e in session.scalars(select(IncidentEvent).order_by(IncidentEvent.occurred_at)).all()]
    assert kinds == ["incident_opened", "alert_resolved", "alerts_cleared"]


def test_partial_resolution_does_not_clear_incident(client, session, factory, settings):
    post(client, am_payload())
    drain(factory, settings)
    post(client, am_payload(fingerprint="def456", alertname="ShopDemoDown"))
    drain(factory, settings)
    post(client, am_payload(status="resolved", ends="2026-11-17T18:10:00Z"))
    drain(factory, settings)
    incident = session.scalars(select(Incident)).one()
    session.refresh(incident)
    assert incident.active_alerts == 1
    kinds = [e.kind for e in session.scalars(select(IncidentEvent)).all()]
    assert "alerts_cleared" not in kinds


def test_processing_failure_is_visible_and_retried(client, session, factory, settings, monkeypatch):
    import app.worker as worker

    post(client, am_payload())

    def boom(*_):
        raise RuntimeError("simulated failure")

    monkeypatch.setitem(worker.HANDLERS, "alert.process", boom)
    run_once(factory, settings)
    job = session.scalars(select(Job)).one()
    session.refresh(job)
    assert job.status == "queued" and job.attempts == 1 and "simulated failure" in job.last_error
    run = session.scalars(select(PipelineRun)).one()
    session.refresh(run)
    assert run.steps[-1].status == "failed"
    assert session.scalars(select(AlertDelivery)).one().processing_status == "received"


# ---------- queue ----------

def test_queue_retry_backoff_and_final_failure(session):
    job = enqueue(session, "org_local", "x", {}, max_attempts=2)
    session.commit()
    claimed = claim_next(session, 60)
    assert claimed.id == job.id and claimed.attempts == 1
    mark_failed(session, job.id, "err1")
    assert session.get(Job, job.id).status == "queued"
    session.get(Job, job.id).run_after = datetime.now(timezone.utc) - timedelta(seconds=1)
    session.commit()
    claim_next(session, 60)
    mark_failed(session, job.id, "err2")
    assert session.get(Job, job.id).status == "failed"


def test_expired_lease_is_recovered(session):
    job = enqueue(session, "org_local", "x", {})
    session.commit()
    claim_next(session, 60)
    session.get(Job, job.id).lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    session.commit()
    assert recover_expired_leases(session) == 1
    assert session.get(Job, job.id).status == "queued"


# ---------- read APIs and tenancy ----------

def test_read_apis_and_tenant_isolation(client, factory, settings):
    post(client, am_payload())
    drain(factory, settings)
    incidents = client.get("/api/v1/incidents").json()
    assert len(incidents) == 1 and incidents[0]["key"] == "INC-1001"
    detail = client.get(f"/api/v1/incidents/{incidents[0]['id']}").json()
    assert len(detail["alerts"]) == 1 and detail["timeline"][0]["kind"] == "incident_opened"
    alerts = client.get("/api/v1/alerts").json()
    assert alerts[0]["status"] == "firing"
    assert len(client.get(f"/api/v1/alerts/{alerts[0]['id']}/deliveries").json()) == 1
    runs = client.get("/api/v1/pipeline/runs").json()
    delivery_run = next(r for r in runs if r["trigger_kind"] == "alert_delivery")
    assert delivery_run["incident_key"] == "INC-1001" and len(delivery_run["steps"]) == 8
    assert any(r["trigger_kind"] == "evidence_collection" for r in runs)

    other = {"X-Nexora-Organization-Id": "org_other"}
    assert client.get("/api/v1/incidents", headers=other).json() == []
    assert client.get("/api/v1/alerts", headers=other).json() == []
    assert client.get(f"/api/v1/incidents/{incidents[0]['id']}", headers=other).status_code == 404


def test_new_alert_after_everything_cleared_opens_new_incident(client, session, factory, settings):
    post(client, am_payload())
    drain(factory, settings)
    post(client, am_payload(status="resolved", ends="2026-11-17T18:10:00Z"))
    drain(factory, settings)
    post(client, am_payload(fingerprint="new1", alertname="ShopDemoHighLatency"))
    drain(factory, settings)
    keys = sorted(i.key for i in session.scalars(select(Incident)).all())
    assert keys == ["INC-1001", "INC-1002"]


def test_new_alert_joining_incident_recollects_evidence(client, session, factory, settings):
    post(client, am_payload())
    run_once(factory, settings)  # alert -> incident, evidence job queued
    post(client, am_payload(fingerprint="pool1", alertname="ShopDemoDbPoolSaturated"))
    run_once(factory, settings)  # first evidence job
    run_once(factory, settings)  # second alert joins the incident
    jobs = session.scalars(select(Job).where(Job.type == "evidence.collect")).all()
    assert len(jobs) == 2


def _iso(minutes_ago: float) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_alert_flapping_rejoins_its_incident(client, session, factory, settings):
    post(client, am_payload())
    drain(factory, settings)
    post(client, am_payload(status="resolved", ends=_iso(1)))
    drain(factory, settings)
    post(client, am_payload(starts=_iso(0)))
    drain(factory, settings)
    incident = session.scalars(select(Incident)).one()
    session.refresh(incident)
    assert incident.active_alerts == 1
    assert "alert_refired" in [e.kind for e in session.scalars(select(IncidentEvent)).all()]


def test_same_alert_recurring_later_opens_new_incident(client, session, factory, settings):
    post(client, am_payload(starts=_iso(120)))
    drain(factory, settings)
    post(client, am_payload(status="resolved", ends=_iso(60)))
    drain(factory, settings)
    post(client, am_payload(starts=_iso(0)))  # same alert, an hour after it cleared
    drain(factory, settings)
    old, new = sorted(session.scalars(select(Incident)).all(), key=lambda i: i.key)
    session.refresh(old)
    session.refresh(new)
    assert (old.key, new.key) == ("INC-1001", "INC-1002")
    assert old.active_alerts == 0 and new.active_alerts == 1
    assert "alert_recurred" in [e.kind for e in session.scalars(select(IncidentEvent)
                                                              .where(IncidentEvent.incident_id == old.id))]
    # the old incident still lists the alert, marked as moved
    detail = client.get(f"/api/v1/incidents/{old.id}").json()
    assert [a["status"] for a in detail["alerts"]] == ["moved"]
    # the recurrence gets its own evidence, and resolving it updates the new incident
    assert len(session.scalars(select(Job).where(Job.type == "evidence.collect")).all()) == 2
    post(client, am_payload(status="resolved", ends=_iso(0)))
    drain(factory, settings)
    session.refresh(new)
    assert new.active_alerts == 0
