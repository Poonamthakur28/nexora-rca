"""step 5: mitigation_executions

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "mitigation_executions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("plan_id", sa.Uuid(), sa.ForeignKey("mitigation_plans.id"), nullable=False),
        sa.Column("service", sa.String(100), nullable=False),
        sa.Column("environment", sa.String(50), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("before_state", JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("result", JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_mitigation_executions_plan", "mitigation_executions", ["plan_id"])
    op.create_index("ix_mitigation_executions_organization_id", "mitigation_executions", ["organization_id"])
    op.create_index(
        "uq_mitigation_execution_running",
        "mitigation_executions",
        ["organization_id", "service", "environment"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
        sqlite_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("uq_mitigation_execution_running", table_name="mitigation_executions")
    op.drop_index("ix_mitigation_executions_organization_id", table_name="mitigation_executions")
    op.drop_index("ix_mitigation_executions_plan", table_name="mitigation_executions")
    op.drop_table("mitigation_executions")
