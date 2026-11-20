"""Durable job queue on the database.

Producers call `enqueue` inside their own transaction, so a job exists if and only if
the data it refers to was committed. The worker claims jobs one at a time with
FOR UPDATE SKIP LOCKED (PostgreSQL), so several workers can run safely.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.db.models import Job


def now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(session: Session, organization_id: str, job_type: str, payload: dict, max_attempts: int = 5,
            run_after: datetime | None = None) -> Job:
    job = Job(organization_id=organization_id, type=job_type, payload=payload, max_attempts=max_attempts,
              run_after=run_after or now())
    session.add(job)
    session.flush()
    return job


def recover_expired_leases(session: Session) -> int:
    """Jobs whose worker died mid-run go back to the queue."""
    result = session.execute(
        update(Job)
        .where(Job.status == "running", Job.lease_until < now())
        .values(status="queued", lease_until=None, updated_at=now())
    )
    session.commit()
    return result.rowcount or 0


def claim_next(session: Session, lease_seconds: int) -> Job | None:
    stmt = (
        select(Job)
        .where(Job.status == "queued", or_(Job.run_after.is_(None), Job.run_after <= now()))
        .order_by(Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = session.scalars(stmt).first()
    if job is None:
        session.rollback()
        return None
    job.status = "running"
    job.attempts += 1
    job.lease_until = now() + timedelta(seconds=lease_seconds)
    job.updated_at = now()
    session.commit()
    return job


def mark_succeeded(session: Session, job_id: uuid.UUID) -> None:
    job = session.get(Job, job_id)
    if job:
        job.status, job.lease_until, job.last_error, job.updated_at = "succeeded", None, None, now()
        session.commit()


def mark_failed(session: Session, job_id: uuid.UUID, error: str) -> Job | None:
    job = session.get(Job, job_id)
    if job is None:
        return None
    job.last_error = error[:1000]
    job.lease_until = None
    job.updated_at = now()
    if job.attempts >= job.max_attempts:
        job.status = "failed"
    else:
        job.status = "queued"
        job.run_after = now() + timedelta(seconds=min(60, 2 ** job.attempts))
    session.commit()
    return job
