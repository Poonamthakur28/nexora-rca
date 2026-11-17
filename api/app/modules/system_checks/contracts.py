"""Generic check result contract, reused by the Pipeline view in later steps."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

CheckStatus = Literal["success", "failed", "warning", "skipped"]
CheckCategory = Literal["component", "signal", "action"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CheckResult(BaseModel):
    id: str
    name: str
    category: CheckCategory
    status: CheckStatus
    latency_ms: int | None = None
    detail: str = ""
    checked_at: datetime = Field(default_factory=utc_now)


class SystemChecksReport(BaseModel):
    overall: CheckStatus
    components: list[CheckResult]
    signals: list[CheckResult]
    checked_at: datetime = Field(default_factory=utc_now)


def overall_status(results: list[CheckResult]) -> CheckStatus:
    statuses = {r.status for r in results}
    if "failed" in statuses:
        return "failed"
    if "warning" in statuses:
        return "warning"
    if statuses <= {"skipped"}:
        return "skipped"
    return "success"
