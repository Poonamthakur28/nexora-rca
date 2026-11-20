"""The worker check reflects a real heartbeat written by app.worker.heartbeat; nothing else makes it green."""

from datetime import timedelta

from app.db.base import utc_now
from app.db.models import WorkerHeartbeat
from app.modules.system_checks.db_checks import run_db_checks
from app.worker import heartbeat


def _worker(factory, settings):
    components, _ = run_db_checks(factory, settings)
    return next(c for c in components if c.id == "worker")


def test_no_worker_started_is_failed(factory, settings):
    result = _worker(factory, settings)
    assert result.status == "failed"
    assert result.detail.startswith("no heartbeat yet")


def test_running_worker_heartbeat_is_success(factory, settings):
    heartbeat(factory, "test-host")
    result = _worker(factory, settings)
    assert result.status == "success"
    assert result.detail.startswith("alive")


def test_heartbeat_updates_same_row(factory, settings):
    heartbeat(factory, "test-host")
    heartbeat(factory, "test-host")
    with factory() as s:
        assert s.query(WorkerHeartbeat).count() == 1


def test_stopped_worker_goes_failed_after_30_seconds(factory, settings):
    with factory() as s:
        s.add(WorkerHeartbeat(worker_id="old-host", beat_at=utc_now() - timedelta(seconds=45)))
        s.commit()
    result = _worker(factory, settings)
    assert result.status == "failed"
    assert result.detail == "last heartbeat 45 s ago"
