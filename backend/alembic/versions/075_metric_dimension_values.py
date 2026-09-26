"""Add refreshable dimension values for published metric versions.

Revision ID: d4b7c2e9a075
Revises: c91f40a73b02
Create Date: 2026-09-26
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d4b7c2e9a075"
down_revision = "c91f40a73b02"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "metric_dimension_value",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("metric_version_id", sa.BigInteger(), nullable=False),
        sa.Column("dimension", sa.String(length=255), nullable=False),
        sa.Column("values", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="ok", nullable=False),
        sa.Column("source", sa.String(length=32), server_default="sampled", nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["metric_version_id"], ["metric_version.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("metric_version_id", "dimension", name="uq_metric_dimension_value"),
    )
    op.create_index(
        "ix_metric_dimension_value_metric_version_id",
        "metric_dimension_value",
        ["metric_version_id"],
    )


def downgrade():
    op.drop_index("ix_metric_dimension_value_metric_version_id", table_name="metric_dimension_value")
    op.drop_table("metric_dimension_value")
