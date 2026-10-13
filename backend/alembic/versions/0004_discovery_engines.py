"""discovery intelligence engines: graph, signals, snapshots, document records, completeness, effectiveness, monitors

The discovery tables were added to ``app.models`` without a migration; this revision creates any that are missing (it is
safe to run on a database where some already exist) and applies the additive column / index changes of the discovery engines:

* organisation_record_links  + candidate_org_id, detail, ix_orglink_status      (DISC-12 review queue)
* organisation_completeness  + gap_log, gap_exhausted_at, needs_review          (DISC-13)
* source_effectiveness       + partial unique index for the all-time (run_id IS NULL) row
* organisations              + ix_org_trading_prefix, ix_org_country_postcode, ix_org_sic_gin

Revision ID: 0004_discovery_engines
Revises: 0002_org_sources
"""
from alembic import op

revision = "0004_discovery_engines"
down_revision = "0002_org_sources"
branch_labels = None
depends_on = None

TABLES = (
    "business_relationships", "organisation_record_links", "business_signals", "website_snapshots", "document_records",
    "organisation_completeness", "source_effectiveness", "monitor_schedules",
)


def upgrade() -> None:
    from app.models import Base

    bind = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(bind, checkfirst=True)
    # columns / indexes for databases where the tables already existed in an older shape
    op.execute("ALTER TABLE organisation_record_links ADD COLUMN IF NOT EXISTS candidate_org_id BIGINT REFERENCES organisations(id) ON DELETE CASCADE")
    op.execute("ALTER TABLE organisation_record_links ADD COLUMN IF NOT EXISTS detail JSONB NOT NULL DEFAULT '{}'::jsonb")
    op.execute("CREATE INDEX IF NOT EXISTS ix_organisation_record_links_candidate_org_id ON organisation_record_links (candidate_org_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_orglink_status ON organisation_record_links (status, id)")
    op.execute("ALTER TABLE organisation_completeness ADD COLUMN IF NOT EXISTS gap_log JSONB NOT NULL DEFAULT '[]'::jsonb")
    op.execute("ALTER TABLE organisation_completeness ADD COLUMN IF NOT EXISTS gap_exhausted_at TIMESTAMPTZ")
    op.execute("ALTER TABLE organisation_completeness ADD COLUMN IF NOT EXISTS needs_review BOOLEAN NOT NULL DEFAULT false")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_srceff_global ON source_effectiveness (adapter_id) WHERE run_id IS NULL")
    op.execute("CREATE INDEX IF NOT EXISTS ix_org_trading_prefix ON organisations (lower(trading_name) text_pattern_ops)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_org_country_postcode ON organisations (country_code, postal_code)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_org_sic_gin ON organisations USING gin (sic_codes)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_org_sic_gin")
    op.execute("DROP INDEX IF EXISTS ix_org_country_postcode")
    op.execute("DROP INDEX IF EXISTS ix_org_trading_prefix")
    op.execute("DROP INDEX IF EXISTS uq_srceff_global")
    op.execute("ALTER TABLE organisation_completeness DROP COLUMN IF EXISTS needs_review")
    op.execute("ALTER TABLE organisation_completeness DROP COLUMN IF EXISTS gap_exhausted_at")
    op.execute("ALTER TABLE organisation_completeness DROP COLUMN IF EXISTS gap_log")
    op.execute("DROP INDEX IF EXISTS ix_orglink_status")
    op.execute("ALTER TABLE organisation_record_links DROP COLUMN IF EXISTS detail")
    op.execute("ALTER TABLE organisation_record_links DROP COLUMN IF EXISTS candidate_org_id")
    # the discovery tables themselves are left in place (they predate this revision's column changes)
