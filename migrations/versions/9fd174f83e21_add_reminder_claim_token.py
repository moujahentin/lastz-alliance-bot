"""add reminder claim token

Revision ID: 9fd174f83e21
Revises: 72c03cfe92ad
"""

from alembic import op
import sqlalchemy as sa


revision = "9fd174f83e21"
down_revision = "72c03cfe92ad"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing claimed/sent/skipped rows remain terminal without a token.
    op.add_column("event_reminders", sa.Column("claim_token", sa.String(32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("event_reminders") as batch_op:
        batch_op.drop_column("claim_token")
