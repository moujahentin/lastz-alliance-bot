"""Reporting deadlines and terminal targeted RSVP reminder claims.

Revision ID: a25d49c036e1
Revises: f14c38b925d0
"""
from alembic import op
import sqlalchemy as sa

revision = 'a25d49c036e1'
down_revision = 'f14c38b925d0'
branch_labels = None
depends_on = None
TABLES = (('event_series','series'),('weekly_schedules','schedule'),('events','occurrence'))


def upgrade():
    # As in earlier migrations, SQLite batch rebuilds use Alembic's FK-off
    # connection; production application connections continue enforcing FKs.
    for table,label in TABLES:
        occurrence = table == 'events'
        column = 'rsvp_deadline' if occurrence else 'deadline_minutes'
        rule = 'rsvp_deadline IS NULL OR rsvp_deadline < starts_at' if occurrence else 'deadline_minutes IS NULL OR deadline_minutes > 0'
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column(column, sa.DateTime() if occurrence else sa.Integer(), nullable=True))
            batch.add_column(sa.Column('missing_reminder',sa.Boolean(),nullable=False,server_default='0'))
            batch.create_check_constraint(f'ck_{label}_deadline',rule)
            batch.create_check_constraint(f'ck_{label}_missing_deadline',f'missing_reminder = 0 OR {column} IS NOT NULL')
            if occurrence:
                batch.add_column(sa.Column('deadline_overridden',sa.Boolean(),nullable=False,server_default='0'))
    op.create_table('rsvp_reminders',
        sa.Column('event_id',sa.Integer(),sa.ForeignKey('events.id',ondelete='CASCADE'),primary_key=True),
        sa.Column('discord_user_id',sa.BigInteger(),primary_key=True),
        sa.Column('member_id',sa.Integer(),sa.ForeignKey('members.id',ondelete='CASCADE'),nullable=False),
        sa.Column('claim_token',sa.String(32),nullable=True),
        sa.Column('status',sa.String(20),nullable=False),
        sa.Column('recorded_at',sa.DateTime(),nullable=False),
        sa.Column('sent_at',sa.DateTime(),nullable=True),
        sa.UniqueConstraint('event_id','member_id',name='uq_rsvp_reminder_member'),
        sa.CheckConstraint("status IN ('claimed', 'attempted', 'sent')",name='ck_rsvp_reminder_status'))
    if op.get_bind().exec_driver_sql('PRAGMA foreign_key_check').fetchall():
        raise RuntimeError('Foreign key integrity failed during RSVP deadline migration')


def downgrade():
    connection = op.get_bind()
    checks = ['SELECT COUNT(*) FROM rsvp_reminders', 'SELECT COUNT(*) FROM events WHERE deadline_overridden=1']
    for table,_ in TABLES:
        column='rsvp_deadline' if table=='events' else 'deadline_minutes'
        checks.append(f'SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL OR missing_reminder != 0')
    if any(connection.scalar(sa.text(query)) for query in checks):
        raise RuntimeError('Cannot downgrade while RSVP deadlines or reminder attempts exist.')
    op.drop_table('rsvp_reminders')
    for table,label in reversed(TABLES):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f'ck_{label}_deadline',type_='check')
            batch.drop_constraint(f'ck_{label}_missing_deadline',type_='check')
            batch.drop_column('rsvp_deadline' if table=='events' else 'deadline_minutes')
            batch.drop_column('missing_reminder')
            if table=='events': batch.drop_column('deadline_overridden')
