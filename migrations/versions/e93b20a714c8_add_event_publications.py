"""persist concrete occurrence Discord cards

Revision ID: e93b20a714c8
Revises: d82a19f603b7
"""
from alembic import op
import sqlalchemy as sa

revision = "e93b20a714c8"
down_revision = "d82a19f603b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_publications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("message_id"),
        sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_event_publications_event_id", "event_publications", ["event_id"])


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM event_publications")):
        raise RuntimeError("Cannot downgrade while event publications exist; remove published cards first.")
    op.drop_table("event_publications")
