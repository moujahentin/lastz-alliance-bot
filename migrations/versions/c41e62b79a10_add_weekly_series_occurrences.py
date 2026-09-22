"""add weekly series and occurrence history without changing legacy event IDs

Revision ID: c41e62b79a10
Revises: 9fd174f83e21
"""

from alembic import op
import sqlalchemy as sa


revision = "c41e62b79a10"
down_revision = "9fd174f83e21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_series",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("alliance_id", sa.Integer(), sa.ForeignKey("alliances.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("created_by_discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("id", "alliance_id", name="uq_series_id_alliance"),
    )
    op.create_index("ix_event_series_alliance_id", "event_series", ["alliance_id"])
    op.create_table(
        "weekly_schedules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("series_id", sa.Integer(), sa.ForeignKey("event_series.id", ondelete="CASCADE"), nullable=False),
        sa.Column("anchor_at", sa.DateTime(), nullable=False),
        sa.Column("next_slot_at", sa.DateTime(), nullable=False),
        sa.Column("ends_at", sa.DateTime(), nullable=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.UniqueConstraint("id", "series_id", name="uq_schedule_id_series"),
    )
    op.create_index("ix_weekly_schedules_series_id", "weekly_schedules", ["series_id"])
    op.create_index("uq_weekly_schedule_open", "weekly_schedules", ["series_id"],
                    unique=True, sqlite_where=sa.text("ends_at IS NULL"))
    with op.batch_alter_table("events") as batch:
        batch.add_column(sa.Column("series_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("schedule_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("nominal_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("status", sa.String(20), server_default="scheduled", nullable=False))
        batch.add_column(sa.Column("is_exception", sa.Boolean(), server_default="0", nullable=False))
        batch.create_index("ix_events_series_id", ["series_id"])
        batch.create_foreign_key("fk_occurrence_series_tenant", "event_series",
                                 ["series_id", "alliance_id"], ["id", "alliance_id"], ondelete="RESTRICT")
        batch.create_foreign_key("fk_occurrence_schedule_series", "weekly_schedules",
                                 ["schedule_id", "series_id"], ["id", "series_id"], ondelete="RESTRICT")
        batch.create_unique_constraint("uq_occurrence_schedule_slot", ["schedule_id", "nominal_at"])
        batch.create_check_constraint("ck_occurrence_series_slot",
            "(series_id IS NULL AND schedule_id IS NULL AND nominal_at IS NULL) OR "
            "(series_id IS NOT NULL AND schedule_id IS NOT NULL AND nominal_at IS NOT NULL)")
        batch.create_check_constraint("ck_occurrence_status", "status IN ('scheduled', 'completed', 'cancelled')")


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM event_series")):
        raise RuntimeError("Cannot downgrade while weekly series/history exist; preserve or export them first.")
    with op.batch_alter_table("events") as batch:
        batch.drop_constraint("fk_occurrence_series_tenant", type_="foreignkey")
        batch.drop_constraint("fk_occurrence_schedule_series", type_="foreignkey")
        batch.drop_constraint("uq_occurrence_schedule_slot", type_="unique")
        batch.drop_constraint("ck_occurrence_series_slot", type_="check")
        batch.drop_constraint("ck_occurrence_status", type_="check")
        batch.drop_index("ix_events_series_id")
        for column in ("series_id", "schedule_id", "nominal_at", "status", "is_exception"):
            batch.drop_column(column)
    op.drop_table("weekly_schedules")
    op.drop_table("event_series")
