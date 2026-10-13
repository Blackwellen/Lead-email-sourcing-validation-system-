"""email prefix index for global search

Revision ID: 0003_email_prefix_index
Revises: 0002_totp_last_counter
"""
import sqlalchemy as sa

from alembic import op

revision = "0003_email_prefix_index"
down_revision = "0002_totp_last_counter"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_email_prefix", "email_addresses", [sa.text("email_normalised text_pattern_ops")], unique=False)


def downgrade() -> None:
    op.drop_index("ix_email_prefix", table_name="email_addresses")
