"""organisation_sources (per-record provenance, licence, attribution)

Revision ID: 0002_org_sources
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_org_sources"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organisation_sources",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("organisation_id", sa.BigInteger(), nullable=False),
        sa.Column("adapter_id", sa.String(length=80), nullable=False),
        sa.Column("source_ref", sa.String(length=300), server_default=sa.text("''"), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("licence", sa.String(length=200), nullable=True),
        sa.Column("attribution", sa.Text(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("website", sa.Text(), nullable=True),
        sa.Column("email_published", sa.String(length=320), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["sourcing_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organisation_id", "adapter_id", "source_ref", name="uq_orgsource"),
    )
    op.create_index(op.f("ix_organisation_sources_organisation_id"), "organisation_sources", ["organisation_id"])


def downgrade() -> None:
    op.drop_table("organisation_sources")
