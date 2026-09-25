"""add participation snapshots and occurrence RSVP intentions

Revision ID: d82a19f603b7
Revises: c41e62b79a10
"""
from alembic import op
import sqlalchemy as sa

revision = "d82a19f603b7"
down_revision = "c41e62b79a10"
branch_labels = None
depends_on = None

TABLES = (("event_series", "series"), ("weekly_schedules", "schedule"), ("events", "occurrence"))


def upgrade() -> None:
    for table, label in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("participation", sa.String(20), nullable=False, server_default="none"))
            batch.create_check_constraint(f"ck_{label}_participation",
                                          "participation IN ('none', 'optional', 'required')")
            if table == "events":
                batch.add_column(sa.Column("participation_overridden", sa.Boolean(),
                                           nullable=False, server_default="0"))
    op.create_table(
        "event_rsvps",
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("discord_user_id", sa.BigInteger(), primary_key=True),
        sa.Column("response", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("response IN ('going', 'not_going', 'maybe')", name="ck_event_rsvps_response"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    # Refuse silent loss of intentions or participation configuration/history.
    if connection.scalar(sa.text("SELECT COUNT(*) FROM event_rsvps")) or any(
        connection.scalar(sa.text(f"SELECT COUNT(*) FROM {table} WHERE participation != 'none'"))
        for table, _ in TABLES
    ) or connection.scalar(sa.text("SELECT COUNT(*) FROM events WHERE participation_overridden = 1")):
        raise RuntimeError("Cannot downgrade while RSVP or participation data exist.")
    op.drop_table("event_rsvps")
    for table, label in reversed(TABLES):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"ck_{label}_participation", type_="check")
            batch.drop_column("participation")
            if table == "events":
                batch.drop_column("participation_overridden")
