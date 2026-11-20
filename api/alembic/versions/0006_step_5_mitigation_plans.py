"""step 5: mitigation_plans

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "mitigation_plans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("rca_report_id", sa.Uuid(), sa.ForeignKey("rca_reports.id"), nullable=True),
        sa.Column("action_type", sa.String(50), nullable=False),
        sa.Column("params", JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("risk", sa.String(16), nullable=False),
        sa.Column("approval_mode", sa.String(16), nullable=False),
        sa.Column("gate_results", JSON, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="proposed"),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("undo_plan", JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("decided_by", sa.String(100), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mitigation_plans_incident_created", "mitigation_plans", ["incident_id", "created_at"])
    op.create_index("ix_mitigation_plans_organization_id", "mitigation_plans", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_mitigation_plans_organization_id", table_name="mitigation_plans")
    op.drop_index("ix_mitigation_plans_incident_created", table_name="mitigation_plans")
    op.drop_table("mitigation_plans")
