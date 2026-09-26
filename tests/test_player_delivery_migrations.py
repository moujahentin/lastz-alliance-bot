"""Upgrade a populated PR9 database without activating any delivery."""
import unittest

from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

import test_rsvp_deadline_migrations as previous


class PlayerDeliveryMigrationTests(unittest.TestCase):
    snapshot = previous.RSVPDeadlineMigrationTests.snapshot
    publication = previous.RSVPDeadlineMigrationTests.publication
    attempt = previous.RSVPDeadlineMigrationTests.attempt

    def setUp(self):
        previous.RSVPDeadlineMigrationTests.setUp(self)
        command.upgrade(self.config, 'a25d49c036e1')
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE events SET rsvp_deadline='2026-09-25 18:00:00', missing_reminder=1 WHERE id=1"))
            connection.execute(text('UPDATE event_series SET deadline_minutes=90, missing_reminder=1 WHERE id=1'))
            connection.execute(text('UPDATE weekly_schedules SET deadline_minutes=90, missing_reminder=1 WHERE id=1'))
        self.attempt(status='attempted')
        for table in inspect(self.engine).get_table_names():
            if table != 'alembic_version':
                self.columns[table] = [c['name'] for c in inspect(self.engine).get_columns(table)]
        self.before = self.snapshot()

    def test_populated_pr9_upgrade_preserves_every_table_and_defaults(self):
        command.upgrade(self.config, 'head')
        self.assertEqual(self.snapshot(), self.before)
        with self.engine.connect() as connection:
            self.assertEqual(connection.scalar(text('SELECT COUNT(*) FROM alliances WHERE auto_publish != 0 OR player_reminder_mask != 0')), 0)
            for table in ('automatic_publications', 'player_reminders'):
                self.assertEqual(connection.scalar(text(f'SELECT COUNT(*) FROM {table}')), 0)
            self.assertEqual(connection.execute(text('PRAGMA foreign_key_check')).all(), [])
        command.current(self.config); command.check(self.config)
        self.assertIn('b36e50d147f2 (head)', self.config.stdout.getvalue())
        self.assertIn('No new upgrade operations detected', self.config.stdout.getvalue())

    def test_roundtrip_retains_pr9_policy_and_claims(self):
        command.upgrade(self.config, 'head'); command.downgrade(self.config, 'a25d49c036e1')
        self.assertEqual(self.snapshot(), self.before)
        command.upgrade(self.config, 'head'); command.check(self.config)

    def test_constraints_cascade_and_publication_cleanup_keeps_terminal_attempt(self):
        command.upgrade(self.config, 'head')
        with self.engine.begin() as connection:
            connection.execute(text("INSERT INTO automatic_publications (event_id,publication_id,attempted,recorded_at) SELECT 1,id,1,'2026-09-25 17:00:00' FROM event_publications WHERE message_id=1000"))
            connection.execute(text("INSERT INTO player_reminders (event_id,discord_user_id,lead_minutes,member_id,status,recorded_at) VALUES (1,30,60,1,'claimed','2026-09-25 17:00:00')"))
        for query in ('UPDATE alliances SET player_reminder_mask=8 WHERE id=1',
                      'UPDATE player_reminders SET lead_minutes=30',
                      "UPDATE player_reminders SET status='retry'",
                      'UPDATE player_reminders SET member_id=999'):
            with self.assertRaises(IntegrityError), self.engine.begin() as connection:
                connection.execute(text(query))
        with self.engine.begin() as connection:
            connection.execute(text('DELETE FROM event_publications WHERE message_id=1000'))
            self.assertIsNone(connection.scalar(text('SELECT publication_id FROM automatic_publications WHERE event_id=1')))
            self.assertEqual(connection.scalar(text('SELECT COUNT(*) FROM automatic_publications')), 1)
            connection.execute(text('DELETE FROM events WHERE id=1'))
            self.assertEqual(connection.scalar(text('SELECT COUNT(*) FROM automatic_publications')), 0)
            self.assertEqual(connection.scalar(text('SELECT COUNT(*) FROM player_reminders')), 0)
            self.assertEqual(connection.execute(text('PRAGMA foreign_key_check')).all(), [])

    def test_downgrade_refuses_new_settings_and_attempts(self):
        command.upgrade(self.config, 'head')
        with self.engine.begin() as connection:
            connection.execute(text('UPDATE alliances SET auto_publish=1 WHERE id=1'))
        with self.assertRaisesRegex(RuntimeError, 'player delivery'): command.downgrade(self.config, 'a25d49c036e1')
        with self.engine.begin() as connection:
            connection.execute(text('UPDATE alliances SET auto_publish=0 WHERE id=1'))
            connection.execute(text("INSERT INTO automatic_publications (event_id,recorded_at) VALUES (1,'2026-09-25 17:00:00')"))
        with self.assertRaisesRegex(RuntimeError, 'player delivery'): command.downgrade(self.config, 'a25d49c036e1')
