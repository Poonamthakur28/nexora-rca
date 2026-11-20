"""Records each pipeline stage as a success/failed/warning step, for the Pipeline view."""

import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import PipelineRun, PipelineStep

STATUS_RANK = {"success": 0, "skipped": 0, "warning": 1, "failed": 2}


class PipelineRecorder:
    def __init__(self, session: Session, run: PipelineRun):
        self.session = session
        self.run = run
        self._t = time.perf_counter()
        self._started = datetime.now(timezone.utc)

    @classmethod
    def start(cls, session: Session, organization_id: str, trigger_kind: str, title: str,
              trigger_id: str | None = None) -> "PipelineRecorder":
        run = PipelineRun(organization_id=organization_id, trigger_kind=trigger_kind, title=title,
                          trigger_id=trigger_id, status="running")
        session.add(run)
        session.flush()
        return cls(session, run)

    @classmethod
    def resume(cls, session: Session, run_id: uuid.UUID) -> "PipelineRecorder | None":
        run = session.get(PipelineRun, run_id)
        return cls(session, run) if run else None

    def _next_seq(self) -> int:
        current = self.session.scalar(
            select(func.max(PipelineStep.seq)).where(PipelineStep.run_id == self.run.id)
        )
        return (current or 0) + 1

    def step(self, stage: str, name: str, status: str, detail: str = "") -> PipelineStep:
        now = datetime.now(timezone.utc)
        duration = int((time.perf_counter() - self._t) * 1000)
        step = PipelineStep(
            run_id=self.run.id, organization_id=self.run.organization_id, seq=self._next_seq(),
            stage=stage, name=name, status=status, detail=detail[:500],
            started_at=self._started, finished_at=now, duration_ms=duration,
        )
        self.session.add(step)
        self.session.flush()
        self._t = time.perf_counter()
        self._started = now
        return step

    def finish(self, status: str | None = None) -> None:
        if status is None:
            steps = self.session.scalars(select(PipelineStep).where(PipelineStep.run_id == self.run.id)).all()
            worst = max((STATUS_RANK.get(s.status, 0) for s in steps), default=0)
            status = {0: "success", 1: "warning", 2: "failed"}[worst]
        self.run.status = status
        self.run.finished_at = datetime.now(timezone.utc)
