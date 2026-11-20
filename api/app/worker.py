"""Nexora worker: claims jobs from the database queue and runs their handlers.

Run with:  python -m app.worker
"""

import logging
import signal
import socket
import time
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.db.base import get_session_factory, utc_now
from app.db.models import WorkerHeartbeat
from app.jobs.queue import claim_next, mark_failed, mark_succeeded, recover_expired_leases
from app.modules.alert_ingestion.processor import process_delivery, record_processing_failure
from app.modules.evidence.collect import collect_evidence, record_collection_failure
from app.modules.fix.engine import record_fix_failure, run_fix
from app.modules.mitigation.execute import record_execute_failure, run_execute
from app.modules.mitigation.policy import record_plan_failure, run_plan
from app.modules.mitigation.recovery import record_verify_failure, run_verify
from app.modules.rca.engine import investigate, record_investigation_failure

log = logging.getLogger("nexora.worker")


def _alert_process(session: Session, settings: Settings, payload: dict) -> dict:
    return process_delivery(session, settings, payload["delivery_id"], payload.get("run_id"))


def _alert_process_failed(session: Session, payload: dict, error: str, final: bool) -> None:
    record_processing_failure(session, payload["delivery_id"], payload.get("run_id"), error, final)


def _evidence_collect(session: Session, settings: Settings, payload: dict) -> dict:
    return collect_evidence(session, settings, payload["incident_id"], payload.get("run_id"))


def _evidence_collect_failed(session: Session, payload: dict, error: str, final: bool) -> None:
    record_collection_failure(session, payload.get("run_id"), error, final)


def _rca_investigate(session: Session, settings: Settings, payload: dict) -> dict:
    return investigate(session, settings, payload["incident_id"], payload.get("run_id"))


def _rca_investigate_failed(session: Session, payload: dict, error: str, final: bool) -> None:
    record_investigation_failure(session, payload.get("run_id"), error, final)


def _mitigation_plan(session: Session, settings: Settings, payload: dict) -> dict:
    return run_plan(session, settings, payload["incident_id"], payload.get("run_id"))


def _mitigation_plan_failed(session: Session, payload: dict, error: str, final: bool) -> None:
    record_plan_failure(session, payload.get("run_id"), error, final)


def _mitigation_execute(session: Session, settings: Settings, payload: dict) -> dict:
    return run_execute(session, settings, payload["plan_id"], payload.get("run_id"),
                       actor=payload.get("actor") or "local-user")


def _mitigation_execute_failed(session: Session, payload: dict, error: str, final: bool) -> None:
    record_execute_failure(session, payload, error, final)


def _recovery_verify(session: Session, settings: Settings, payload: dict) -> dict:
    return run_verify(session, settings, payload["execution_id"], payload.get("run_id"), payload=payload)


def _recovery_verify_failed(session: Session, payload: dict, error: str, final: bool) -> None:
    record_verify_failure(session, payload.get("run_id"), error, final)


def _fix_propose(session: Session, settings: Settings, payload: dict) -> dict:
    return run_fix(session, settings, payload["attempt_id"], payload.get("run_id"))


def _fix_propose_failed(session: Session, payload: dict, error: str, final: bool) -> None:
    record_fix_failure(session, payload, error, final)


HANDLERS: dict[str, Callable[[Session, Settings, dict], dict]] = {
    "alert.process": _alert_process,
    "evidence.collect": _evidence_collect,
    "rca.investigate": _rca_investigate,
    "mitigation.plan": _mitigation_plan,
    "mitigation.execute": _mitigation_execute,
    "recovery.verify": _recovery_verify,
    "fix.propose": _fix_propose,
}
# Jobs that legitimately run longer than JOB_LEASE_SECONDS (LLM calls + sandbox runs). Without a longer
# lease, recover_expired_leases would hand a still-running job to another worker.
LONG_LEASES: dict[str, Callable[[Settings], int]] = {
    "fix.propose": lambda s: s.fix_job_lease_seconds,
}
FAILURE_HOOKS: dict[str, Callable[[Session, dict, str, bool], None]] = {
    "alert.process": _alert_process_failed,
    "evidence.collect": _evidence_collect_failed,
    "rca.investigate": _rca_investigate_failed,
    "mitigation.plan": _mitigation_plan_failed,
    "mitigation.execute": _mitigation_execute_failed,
    "recovery.verify": _recovery_verify_failed,
    "fix.propose": _fix_propose_failed,
}


def heartbeat(factory: sessionmaker[Session], worker_id: str) -> None:
    with factory() as s:
        beat = s.get(WorkerHeartbeat, worker_id)
        if beat is None:
            s.add(WorkerHeartbeat(worker_id=worker_id, beat_at=utc_now()))
        else:
            beat.beat_at = utc_now()
        s.commit()


def run_once(factory: sessionmaker[Session], settings: Settings) -> bool:
    """Claim and run one job. Returns True if a job was processed."""
    with factory() as s:
        recover_expired_leases(s)
        job = claim_next(s, settings.job_lease_seconds)
        if job is None:
            return False
        job_id, job_type, payload, attempts, max_attempts = job.id, job.type, dict(job.payload), job.attempts, job.max_attempts
        if job_type in LONG_LEASES:
            job.lease_until = utc_now() + timedelta(seconds=LONG_LEASES[job_type](settings))
            s.commit()

    handler = HANDLERS.get(job_type)
    with factory() as s:
        try:
            if handler is None:
                raise LookupError(f"no handler for job type {job_type}")
            result = handler(s, settings, payload)
            mark_succeeded(s, job_id)
            log.info("job %s %s succeeded: %s", job_type, job_id, result)
        except Exception as exc:  # the job fails, the worker keeps running
            s.rollback()
            error = f"{type(exc).__name__}: {exc}"
            final = attempts >= max_attempts
            log.warning("job %s %s failed (attempt %s/%s): %s", job_type, job_id, attempts, max_attempts, error)
            hook = FAILURE_HOOKS.get(job_type)
            if hook:
                try:
                    hook(s, payload, error, final)
                except Exception:
                    s.rollback()
            mark_failed(s, job_id, error)
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    factory = get_session_factory()
    worker_id = f"{socket.gethostname()}"
    stopping = {"flag": False}

    def stop(*_):
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("worker %s started", worker_id)
    last_beat = 0.0
    while not stopping["flag"]:
        try:
            if time.time() - last_beat > 5:
                heartbeat(factory, worker_id)
                last_beat = time.time()
            if not run_once(factory, settings):
                time.sleep(settings.worker_poll_seconds)
        except Exception as exc:  # database down etc.: wait and retry
            log.error("worker loop error: %s", exc)
            time.sleep(3)
    log.info("worker %s stopped", worker_id)


if __name__ == "__main__":
    main()
