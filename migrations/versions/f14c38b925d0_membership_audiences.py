"""Alliance membership lifecycle and exact event audience snapshots.

Revision ID: f14c38b925d0
Revises: e93b20a714c8
"""
from alembic import op
import sqlalchemy as sa

revision = "f14c38b925d0"
down_revision = "e93b20a714c8"
branch_labels = None
depends_on = None
TABLES = (("event_series", "series"), ("weekly_schedules", "schedule"), ("events", "occurrence"))


def upgrade():
    # Alembic's SQLite migration connection has FKs off while batch tables are
    # rebuilt. Application connections keep FK enforcement on. Preserve IDs and
    # all referencing RSVP/publication/reminder rows, then verify FK integrity.
    with op.batch_alter_table("members") as batch:
        batch.drop_constraint("ck_members_rank_valid", type_="check")
        batch.add_column(sa.Column("active", sa.Boolean(), nullable=False, server_default="1"))
        batch.alter_column("rank", existing_type=sa.String(20), server_default="R1")
    op.create_table("membership_changes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("members.id", ondelete="CASCADE"), nullable=False),
        sa.Column("previous_rank", sa.String(20), nullable=True),
        sa.Column("new_rank", sa.String(20), nullable=False),
        sa.Column("previous_active", sa.Boolean(), nullable=True),
        sa.Column("new_active", sa.Boolean(), nullable=False),
        sa.Column("previous_discord_user_id", sa.BigInteger(), nullable=True),
        sa.Column("new_discord_user_id", sa.BigInteger(), nullable=True),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(20), nullable=False))
    op.create_index("ix_membership_changes_member_id", "membership_changes", ["member_id"])
    # A migration baseline, not an invented human action or earlier rank history.
    op.execute("""INSERT INTO membership_changes
        (member_id,previous_rank,new_rank,previous_active,new_active,
         previous_discord_user_id,new_discord_user_id,actor_id,changed_at,source)
        SELECT id,rank,CASE WHEN rank='MEMBER' THEN 'R1' ELSE rank END,NULL,1,
               discord_user_id,discord_user_id,NULL,CURRENT_TIMESTAMP,'migration' FROM members""")
    op.execute("UPDATE members SET rank='R1' WHERE rank='MEMBER'")
    with op.batch_alter_table("members") as batch:
        batch.create_check_constraint("ck_members_rank_valid", "rank IN ('R1','R2','R3','R4','R5')")
    for table, label in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("audience", sa.Integer(), nullable=False, server_default="31"))
            batch.create_check_constraint(f"ck_{label}_audience", "audience BETWEEN 1 AND 31")
            if table == "events":
                batch.add_column(sa.Column("audience_overridden", sa.Boolean(), nullable=False, server_default="0"))
    op.create_table("event_audience_changes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("previous_audience", sa.Integer(), nullable=True),
        sa.Column("new_audience", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("new_audience BETWEEN 1 AND 31", name="ck_audience_change_new"),
        sa.CheckConstraint("previous_audience IS NULL OR previous_audience BETWEEN 1 AND 31", name="ck_audience_change_previous"))
    op.create_index("ix_event_audience_changes_event_id", "event_audience_changes", ["event_id"])
    op.execute("""INSERT INTO event_audience_changes (event_id,previous_audience,new_audience,actor_id,changed_at)
        SELECT id,NULL,31,NULL,CURRENT_TIMESTAMP FROM events WHERE series_id IS NULL""")
    if op.get_bind().exec_driver_sql("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("Foreign key integrity failed during membership/audience migration")


def downgrade():
    connection = op.get_bind()
    checks = [
        "SELECT COUNT(*) FROM membership_changes WHERE source != 'migration'",
        "SELECT COUNT(*) FROM members WHERE rank IN ('R2','R3') OR active=0",
        "SELECT COUNT(*) FROM event_audience_changes WHERE previous_audience IS NOT NULL OR actor_id IS NOT NULL",
        "SELECT COUNT(*) FROM events WHERE audience_overridden=1",
        *(f"SELECT COUNT(*) FROM {table} WHERE audience != 31" for table, _ in TABLES),
    ]
    if any(connection.scalar(sa.text(query)) for query in checks):
        raise RuntimeError("Cannot downgrade while membership or audience changes exist.")
    op.drop_table("event_audience_changes")
    op.drop_table("membership_changes")
    for table, label in reversed(TABLES):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"ck_{label}_audience", type_="check")
            batch.drop_column("audience")
            if table == "events":
                batch.drop_column("audience_overridden")
    with op.batch_alter_table("members") as batch:
        batch.drop_constraint("ck_members_rank_valid", type_="check")
        batch.drop_column("active")
        batch.alter_column("rank", existing_type=sa.String(20), server_default="MEMBER")
    op.execute("UPDATE members SET rank='MEMBER' WHERE rank='R1'")
    with op.batch_alter_table("members") as batch:
        batch.create_check_constraint("ck_members_rank_valid", "rank IN ('MEMBER', 'R4', 'R5')")
