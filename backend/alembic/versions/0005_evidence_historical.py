"""evidence_sources: historical flag + archive timestamp (Common Crawl historical, unconfirmed evidence)

Revision ID: 0005_evidence_historical
Revises: 0004_discovery_engines
"""
from alembic import op

revision = "0005_evidence_historical"
down_revision = "0004_discovery_engines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE evidence_sources ADD COLUMN IF NOT EXISTS historical BOOLEAN NOT NULL DEFAULT false")
    op.execute("ALTER TABLE evidence_sources ADD COLUMN IF NOT EXISTS archive_timestamp TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE evidence_sources DROP COLUMN IF EXISTS archive_timestamp")
    op.execute("ALTER TABLE evidence_sources DROP COLUMN IF EXISTS historical")
