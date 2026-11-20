"""SQLAlchemy models for Step 2: alerts, incidents, jobs and the pipeline view.

Every table carries organization_id; every query filters on it.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JsonType, new_id, utc_now

TS = DateTime(timezone=True)


class AlertIntegration(Base):
    __tablename__ = "alert_integrations"
    __table_args__ = (UniqueConstraint("organization_id", "provider", "name", name="uq_integration_name"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(100))
    # Only a SHA-256 of the shared token is stored, never the token itself.
    token_sha256: Mapped[str] = mapped_column(String(64), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utc_now)


class AlertDelivery(Base):
    __tablename__ = "alert_deliveries"
    __table_args__ = (
        UniqueConstraint("organization_id", "integration_id", "external_delivery_id", name="uq_delivery_identity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(64), index=True)
    integration_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("alert_integrations.id"))
    provider: Mapped[str] = mapped_column(String(32))
    external_delivery_id: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(TS, default=utc_now)
    raw_payload: Mapped[dict] = mapped_column(JsonType)
    alert_count: Mapped[int] = mapped_column(Integer, default=0)
    processing_status: Mapped[str] = mapped_column(String(16), default="received")
    processing_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("organization_id", "integration_id", "external_alert_id", name="uq_alert_identity"),
        Index("ix_alerts_org_status", "organization_id", "normalized_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(64))
    integration_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("alert_integrations.id"))
    provider: Mapped[str] = mapped_column(String(32))
    external_alert_id: Mapped[str] = mapped_column(String(128))
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    service: Mapped[str | None] = mapped_column(String(100), nullable=True)
    environment: Mapped[str | None] = mapped_column(String(50), nullable=True)
    provider_severity: Mapped[str | None] = mapped_column(String(50), nullable=True)
    normalized_severity: Mapped[str] = mapped_column(String(16))
    provider_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    normalized_status: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(TS)
    resolved_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(TS)
    received_at: Mapped[datetime] = mapped_column(TS, default=utc_now)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (Index("ix_incidents_org_service_env", "organization_id", "service", "environment", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(64))
    key: Mapped[str] = mapped_column(String(16), unique=True)
    title: Mapped[str] = mapped_column(String(300))
    service: Mapped[str | None] = mapped_column(String(100), nullable=True)
    environment: Mapped[str | None] = mapped_column(String(50), nullable=True)
    severity: Mapped[str] = mapped_column(String(16))
    # open -> investigating -> mitigated -> fixing -> resolved; escalated from any state.
    status: Mapped[str] = mapped_column(String(16), default="open")
    active_alerts: Mapped[int] = mapped_column(Integer, default=0)
    detected_at: Mapped[datetime] = mapped_column(TS, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utc_now)

    events: Mapped[list["IncidentEvent"]] = relationship(order_by="IncidentEvent.occurred_at")


class AlertIncidentLink(Base):
    __tablename__ = "alert_incident_links"
    __table_args__ = (UniqueConstraint("alert_id", "incident_id", name="uq_alert_incident"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(64), index=True)
    alert_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("alerts.id"))
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"), index=True)
    reason: Mapped[str] = mapped_column(String(200))
    linked_at: Mapped[datetime] = mapped_column(TS, default=utc_now)
    # Set when the alert recurs later and moves to a new incident; the old incident keeps the history.
    ended_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)


class IncidentEvent(Base):
    __tablename__ = "incident_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(64))
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"), index=True)
    occurred_at: Mapped[datetime] = mapped_column(TS, default=utc_now)
    kind: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JsonType, default=dict)


class Job(Base):
    """Durable job queue (claimed with SELECT ... FOR UPDATE SKIP LOCKED on PostgreSQL)."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "run_after"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict] = mapped_column(JsonType, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|running|succeeded|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[datetime] = mapped_column(TS, default=utc_now)
    lease_until: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utc_now)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    beat_at: Mapped[datetime] = mapped_column(TS, default=utc_now)


class PipelineRun(Base):
    """One pass of the pipeline for one trigger (in Step 2: one webhook delivery)."""

    __tablename__ = "pipeline_runs"
    __table_args__ = (Index("ix_pipeline_runs_org_started", "organization_id", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(64))
    trigger_kind: Mapped[str] = mapped_column(String(32))
    trigger_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|success|failed|warning
    incident_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    started_at: Mapped[datetime] = mapped_column(TS, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)

    steps: Mapped[list["PipelineStep"]] = relationship(order_by="PipelineStep.seq", cascade="all, delete-orphan")


class PipelineStep(Base):
    __tablename__ = "pipeline_steps"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pipeline_runs.id"), index=True)
    organization_id: Mapped[str] = mapped_column(String(64))
    seq: Mapped[int] = mapped_column(Integer)
    stage: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(16))  # success|failed|warning|skipped
    detail: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(TS, default=utc_now)
    finished_at: Mapped[datetime] = mapped_column(TS, default=utc_now)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)


class Service(Base):
    """Service catalog entry: links a running service to its source, owners and telemetry name."""

    __tablename__ = "services"
    __table_args__ = (UniqueConstraint("organization_id", "name", "environment", name="uq_service_env"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(100))
    environment: Mapped[str] = mapped_column(String(50))
    owners: Mapped[list] = mapped_column(JsonType, default=list)
    repository_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Where Nexora can read this service's source code, and the path prefix used inside the
    # running container (stack frames like /srv/app/pricing.py map to <source_root>/app/pricing.py).
    source_root: Mapped[str | None] = mapped_column(String(300), nullable=True)
    runtime_path_prefix: Mapped[str | None] = mapped_column(String(200), nullable=True)
    telemetry_service_name: Mapped[str] = mapped_column(String(100))
    dependencies: Mapped[list] = mapped_column(JsonType, default=list)
    # language, app_namespaces, metric queries: see deploy/config/service-catalog.yaml
    config: Mapped[dict] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(TS, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utc_now)


class ServiceDeployment(Base):
    """A deploy marker: which version and commit went live, and which files it changed."""

    __tablename__ = "service_deployments"
    __table_args__ = (Index("ix_deployments_service_time", "service_id", "deployed_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(64))
    service_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("services.id"))
    version: Mapped[str] = mapped_column(String(50))
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    changed_files: Mapped[list] = mapped_column(JsonType, default=list)
    source: Mapped[str] = mapped_column(String(40), default="manual")  # catalog_seed | test_bench | ci
    has_irreversible_migration: Mapped[bool] = mapped_column(Boolean, default=False)
    deployed_at: Mapped[datetime] = mapped_column(TS, default=utc_now)


class Evidence(Base):
    """One fact collected for an incident. Gaps (a source that could not be read) are evidence too."""

    __tablename__ = "evidence"
    __table_args__ = (Index("ix_evidence_incident_collection", "incident_id", "collection_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(64))
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"))
    collection_id: Mapped[uuid.UUID] = mapped_column(Uuid)  # the pipeline run that collected it
    kind: Mapped[str] = mapped_column(String(20))  # trace | log | metric | deployment | code | gap
    source: Mapped[str] = mapped_column(String(40))  # tempo | loki | prometheus | catalog | source
    title: Mapped[str] = mapped_column(String(300))
    summary: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[dict] = mapped_column(JsonType, default=dict)
    relevance: Mapped[float] = mapped_column(default=0.5)
    observed_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(TS, default=utc_now)


class RcaReport(Base):
    """One AI investigation of an incident, based on one evidence collection. The latest one is shown."""

    __tablename__ = "rca_reports"
    __table_args__ = (Index("ix_rca_reports_incident_created", "incident_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(64))
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"))
    collection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)  # evidence it was based on
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(16))  # succeeded | failed
    category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    grounded: Mapped[bool] = mapped_column(default=False)
    report: Mapped[dict] = mapped_column(JsonType, default=dict)
    checks: Mapped[list] = mapped_column(JsonType, default=list)  # grounding checks: [{check, ok, detail}]
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utc_now)


class MitigationPlan(Base):
    """One catalog action evaluated for an incident. Only the highest-scoring passing plan is proposed."""

    __tablename__ = "mitigation_plans"
    __table_args__ = (Index("ix_mitigation_plans_incident_created", "incident_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(64), index=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"))
    rca_report_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("rca_reports.id"), nullable=True)
    action_type: Mapped[str] = mapped_column(String(50))
    params: Mapped[dict] = mapped_column(JsonType, default=dict)
    title: Mapped[str] = mapped_column(String(300))
    risk: Mapped[str] = mapped_column(String(16))  # low | medium | high
    approval_mode: Mapped[str] = mapped_column(String(16))  # auto | single
    gate_results: Mapped[list] = mapped_column(JsonType, default=list)  # [{gate, ok, detail}]
    score: Mapped[float] = mapped_column(default=0.0)
    # proposed|blocked|approved|rejected|executing|succeeded|failed|undone|expired
    status: Mapped[str] = mapped_column(String(16), default="proposed")
    rationale: Mapped[str] = mapped_column(Text, default="")
    undo_plan: Mapped[dict] = mapped_column(JsonType, default=dict)
    decided_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utc_now)


class MitigationExecution(Base):
    """One attempt to run a catalog action. At most one running execution per service+environment."""

    __tablename__ = "mitigation_executions"
    __table_args__ = (
        Index("ix_mitigation_executions_plan", "plan_id"),
        Index(
            "uq_mitigation_execution_running",
            "organization_id",
            "service",
            "environment",
            unique=True,
            sqlite_where=text("status = 'running'"),
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(64), index=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("mitigation_plans.id"))
    service: Mapped[str] = mapped_column(String(100))
    environment: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|succeeded|failed|undone
    before_state: Mapped[dict] = mapped_column(JsonType, default=dict)
    result: Mapped[dict] = mapped_column(JsonType, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(TS, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)


class FixAttempt(Base):
    """Step 6: one attempt to reproduce an incident's defect in a test and fix it with a verified patch."""

    __tablename__ = "fix_attempts"
    __table_args__ = (Index("ix_fix_attempts_incident_created", "incident_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(64), index=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"))
    rca_report_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("rca_reports.id"), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # queued|running|skipped|repro_failed|patch_rejected|verify_failed|verified|pr_opened|failed
    status: Mapped[str] = mapped_column(String(20), default="queued")
    reason: Mapped[str] = mapped_column(Text, default="")  # why it stopped, shown to responders
    provider: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(100), default="")
    target_file: Mapped[str | None] = mapped_column(String(300), nullable=True)
    repro_test_path: Mapped[str | None] = mapped_column(String(300), nullable=True)
    repro_test_code: Mapped[str] = mapped_column(Text, default="")
    patch_diff: Mapped[str] = mapped_column(Text, default="")  # canonical diff of application code + test
    patch_files: Mapped[list] = mapped_column(JsonType, default=list)
    explanation: Mapped[str] = mapped_column(Text, default="")
    gates: Mapped[list] = mapped_column(JsonType, default=list)  # [{gate, ok, detail}]
    runs: Mapped[dict] = mapped_column(JsonType, default=dict)  # sandbox results by name
    artifacts_dir: Mapped[str | None] = mapped_column(String(500), nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    requested_by: Mapped[str] = mapped_column(String(100), default="auto")
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)


class RecoveryCheck(Base):
    """Verdict after watching catalog metrics for a recovery window."""

    __tablename__ = "recovery_checks"
    __table_args__ = (Index("ix_recovery_checks_execution", "execution_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(64), index=True)
    execution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("mitigation_executions.id"))
    window_seconds: Mapped[int] = mapped_column(Integer)
    signals: Mapped[list] = mapped_column(JsonType, default=list)  # [{metric, threshold, before, after, ok}]
    verdict: Mapped[str] = mapped_column(String(16))  # healthy|unhealthy|inconclusive
    checked_at: Mapped[datetime] = mapped_column(TS, default=utc_now)
