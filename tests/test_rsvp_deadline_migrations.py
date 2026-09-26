"""A populated PR #8 upgrade preserves all prior operational/history data."""
import unittest

from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

import test_membership_audience_migrations as previous


class RSVPDeadlineMigrationTests(unittest.TestCase):
    snapshot = previous.MembershipAudienceMigrationTests.snapshot
    publication = previous.MembershipAudienceMigrationTests.publication

    def setUp(self):
        previous.MembershipAudienceMigrationTests.setUp(self)
        command.upgrade(self.config,'f14c38b925d0')
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE members SET active=0 WHERE id=1"))
            connection.execute(text("UPDATE members SET rank='R2' WHERE id=4"))
            connection.execute(text("UPDATE events SET audience=13,participation='required' WHERE id=1"))
            connection.execute(text("UPDATE event_series SET audience=28,participation='required' WHERE id=1"))
            connection.execute(text("UPDATE weekly_schedules SET audience=28,participation='required' WHERE id=1"))
            connection.execute(text("""INSERT INTO membership_changes
                (member_id,previous_rank,new_rank,previous_active,new_active,actor_id,changed_at,source)
                VALUES (1,'R1','R1',1,0,20,'2026-09-25 17:00:00','management')"""))
            connection.execute(text("""INSERT INTO event_audience_changes
                (event_id,previous_audience,new_audience,actor_id,changed_at)
                VALUES (1,31,13,10,'2026-09-25 17:00:00')"""))
        for table in inspect(self.engine).get_table_names():
            if table != 'alembic_version':
                self.columns[table]=[c['name'] for c in inspect(self.engine).get_columns(table)]
        self.before=self.snapshot()

    def test_upgrade_preserves_all_pr8_rows_and_defaults_and_matches_models(self):
        command.upgrade(self.config,'head')
        self.assertEqual(self.snapshot(),self.before)
        with self.engine.connect() as connection:
            for table in ('events','event_series','weekly_schedules'):
                column='rsvp_deadline' if table=='events' else 'deadline_minutes'
                self.assertEqual(connection.scalar(text(f'SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL OR missing_reminder != 0')),0)
            self.assertEqual(connection.scalar(text('SELECT COUNT(*) FROM rsvp_reminders')),0)
            self.assertEqual(connection.execute(text('PRAGMA foreign_key_check')).all(),[])
        command.current(self.config); command.check(self.config)
        self.assertIn(f'{self.head} (head)',self.config.stdout.getvalue())
        self.assertIn('No new upgrade operations detected',self.config.stdout.getvalue())

    def test_baseline_downgrade_roundtrip_preserves_existing_data(self):
        command.upgrade(self.config,'head'); command.downgrade(self.config,'f14c38b925d0')
        self.assertEqual(self.snapshot(),self.before)
        command.upgrade(self.config,'head'); command.check(self.config)
        self.assertEqual(self.snapshot(),self.before)

    def test_deadline_and_relative_policy_constraints(self):
        command.upgrade(self.config,'head')
        for query in ("UPDATE events SET rsvp_deadline=starts_at WHERE id=1",
                      "UPDATE events SET missing_reminder=1 WHERE id=1",
                      "UPDATE event_series SET deadline_minutes=0 WHERE id=1",
                      "UPDATE weekly_schedules SET deadline_minutes=-1 WHERE id=1"):
            with self.assertRaises(IntegrityError),self.engine.begin() as connection:
                connection.execute(text(query))

    def attempt(self,user=30,member=1,status='claimed'):
        with self.engine.begin() as connection:
            connection.execute(text("""INSERT INTO rsvp_reminders
                (event_id,discord_user_id,member_id,claim_token,status,recorded_at)
                VALUES (1,:user,:member,'token',:status,'2026-09-25 17:00:00')"""),
                {'user':user,'member':member,'status':status})

    def test_claim_constraints_and_deletion_preserve_publication_tombstone(self):
        command.upgrade(self.config,'head'); self.attempt(status='attempted')
        for user,member,status in ((30,2,'claimed'),(99,1,'claimed'),(99,999,'claimed'),(99,2,'retry')):
            with self.assertRaises(IntegrityError): self.attempt(user,member,status)
        with self.engine.begin() as connection:
            connection.execute(text('DELETE FROM events WHERE id=1'))
            self.assertEqual(connection.scalar(text('SELECT COUNT(*) FROM rsvp_reminders')),0)
            self.assertIsNone(connection.scalar(text('SELECT event_id FROM event_publications WHERE message_id=1000')))
            self.assertEqual(connection.execute(text('PRAGMA foreign_key_check')).all(),[])

    def test_downgrade_refuses_attempts_and_policy_data(self):
        command.upgrade(self.config,'head')
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE events SET rsvp_deadline='2026-09-25 18:00:00' WHERE id=1"))
        with self.assertRaisesRegex(RuntimeError,'deadlines or reminder attempts'): command.downgrade(self.config,'f14c38b925d0')
        with self.engine.begin() as connection: connection.execute(text('UPDATE events SET rsvp_deadline=NULL WHERE id=1'))
        self.attempt()
        with self.assertRaisesRegex(RuntimeError,'deadlines or reminder attempts'): command.downgrade(self.config,'f14c38b925d0')
        self.assertEqual(self.snapshot(),self.before)
