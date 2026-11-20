"""step 5: recovery_checks

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "recovery_checks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("execution_id", sa.Uuid(), sa.ForeignKey("mitigation_executions.id"), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("signals", JSON, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_recovery_checks_execution", "recovery_checks", ["execution_id"])
    op.create_index("ix_recovery_checks_organization_id", "recovery_checks", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_recovery_checks_organization_id", table_name="recovery_checks")
    op.drop_index("ix_recovery_checks_execution", table_name="recovery_checks")
    op.drop_table("recovery_checks")
