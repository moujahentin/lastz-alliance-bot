"""add persistent event reminders

Revision ID: 72c03cfe92ad
Revises: e84012607d93
"""

from alembic import op
import sqlalchemy as sa


revision = "72c03cfe92ad"
down_revision = "e84012607d93"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "alliances", sa.Column("reminder_channel_id", sa.BigInteger(), nullable=True),
    )
    op.create_table(
        "event_reminders",
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("lead_minutes", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("lead_minutes IN (30, 10)", name="ck_event_reminders_lead"),
        sa.CheckConstraint(
            "status IN ('claimed', 'sent', 'skipped')",
            name="ck_event_reminders_status",
        ),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id", "lead_minutes"),
    )


def downgrade() -> None:
    op.drop_table("event_reminders")
    with op.batch_alter_table("alliances") as batch_op:
        batch_op.drop_column("reminder_channel_id")
