"""step 6: fix_attempts

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "fix_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("rca_report_id", sa.Uuid(), sa.ForeignKey("rca_reports.id"), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("provider", sa.String(32), nullable=False, server_default=""),
        sa.Column("model", sa.String(100), nullable=False, server_default=""),
        sa.Column("target_file", sa.String(300), nullable=True),
        sa.Column("repro_test_path", sa.String(300), nullable=True),
        sa.Column("repro_test_code", sa.Text(), nullable=False, server_default=""),
        sa.Column("patch_diff", sa.Text(), nullable=False, server_default=""),
        sa.Column("patch_files", JSON, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("explanation", sa.Text(), nullable=False, server_default=""),
        sa.Column("gates", JSON, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("runs", JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("artifacts_dir", sa.String(500), nullable=True),
        sa.Column("pr_url", sa.String(500), nullable=True),
        sa.Column("requested_by", sa.String(100), nullable=False, server_default="auto"),
        sa.Column("llm_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_fix_attempts_incident_created", "fix_attempts", ["incident_id", "created_at"])
    op.create_index("ix_fix_attempts_organization_id", "fix_attempts", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_fix_attempts_organization_id", table_name="fix_attempts")
    op.drop_index("ix_fix_attempts_incident_created", table_name="fix_attempts")
    op.drop_table("fix_attempts")
