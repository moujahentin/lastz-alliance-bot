"""Opt-in automatic cards and independent player reminder attempts.

Revision ID: b36e50d147f2
Revises: a25d49c036e1
"""
from alembic import op
import sqlalchemy as sa

revision = "b36e50d147f2"
down_revision = "a25d49c036e1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("alliances") as batch:
        batch.add_column(sa.Column("auto_publish", sa.Boolean(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("player_reminder_mask", sa.Integer(), nullable=False, server_default="0"))
        batch.create_check_constraint("ck_alliance_player_reminders", "player_reminder_mask BETWEEN 0 AND 7")
    op.create_table("automatic_publications",
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("publication_id", sa.Integer(), sa.ForeignKey("event_publications.id", ondelete="SET NULL"), nullable=True, unique=True),
        sa.Column("attempted", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("recorded_at", sa.DateTime(), nullable=False))
    op.create_table("player_reminders",
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("discord_user_id", sa.BigInteger(), primary_key=True),
        sa.Column("lead_minutes", sa.Integer(), primary_key=True),
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("members.id", ondelete="CASCADE"), nullable=False),
        sa.Column("claim_token", sa.String(32), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("event_id", "member_id", "lead_minutes", name="uq_player_reminder_member"),
        sa.CheckConstraint("lead_minutes IN (1440, 60, 15)", name="ck_player_reminder_lead"),
        sa.CheckConstraint("status IN ('claimed', 'attempted', 'sent', 'skipped')", name="ck_player_reminder_status"))
    if op.get_bind().exec_driver_sql("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("Foreign key integrity failed during player delivery migration")


def downgrade():
    connection = op.get_bind()
    queries = ("SELECT COUNT(*) FROM automatic_publications", "SELECT COUNT(*) FROM player_reminders",
               "SELECT COUNT(*) FROM alliances WHERE auto_publish != 0 OR player_reminder_mask != 0")
    if any(connection.scalar(sa.text(query)) for query in queries):
        raise RuntimeError("Cannot downgrade while player delivery settings or attempts exist.")
    op.drop_table("player_reminders")
    op.drop_table("automatic_publications")
    with op.batch_alter_table("alliances") as batch:
        batch.drop_constraint("ck_alliance_player_reminders", type_="check")
        batch.drop_column("player_reminder_mask")
        batch.drop_column("auto_publish")
