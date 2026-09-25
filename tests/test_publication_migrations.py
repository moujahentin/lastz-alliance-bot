"""Publication migration preserves PR #6 state and deletion cleanup identity."""
import unittest

from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

import test_participation_migrations as previous


class PublicationMigrationTests(unittest.TestCase):
    snapshot = previous.ParticipationMigrationTests.snapshot

    def setUp(self):
        previous.ParticipationMigrationTests.setUp(self)
        command.upgrade(self.config, "d82a19f603b7")
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE events SET participation='optional' WHERE id=1"))
            connection.execute(text(
                "INSERT INTO event_rsvps VALUES (1,30,'going','2026-09-25 18:00:00.000000','2026-09-25 18:01:00.000000')"
            ))
        self.columns = {table: [c["name"] for c in inspect(self.engine).get_columns(table)]
                        for table in (*self.columns, "event_rsvps")}
        self.before = self.snapshot()

    def publication(self, message=1000, occurrence=1, guild=1):
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO event_publications (message_id,event_id,guild_id,channel_id,created_at) "
                "VALUES (:message,:event,:guild,101,'2026-09-25 18:00:00.000000')"
            ), {"message": message, "event": occurrence, "guild": guild})

    def test_upgrade_preserves_occurrences_rsvps_and_reminders_and_matches_models(self):
        command.upgrade(self.config, "head")
        self.assertEqual(self.snapshot(), self.before)
        with self.engine.connect() as connection:
            self.assertEqual(connection.scalar(text("SELECT COUNT(*) FROM event_publications")), 0)
            self.assertEqual(connection.execute(text("PRAGMA foreign_key_check")).all(), [])
        command.current(self.config)
        command.check(self.config)
        self.assertIn("e93b20a714c8 (head)", self.config.stdout.getvalue())
        self.assertIn("No new upgrade operations detected", self.config.stdout.getvalue())

    def test_multiple_cards_uniqueness_and_foreign_keys(self):
        command.upgrade(self.config, "head")
        self.publication()
        self.publication(message=1001)
        for message, occurrence, guild in ((1000, 1, 1), (1002, 999, 1), (1002, 1, 999)):
            with self.assertRaises(IntegrityError):
                self.publication(message, occurrence, guild)
        with self.engine.connect() as connection:
            self.assertEqual(connection.scalar(text("SELECT COUNT(*) FROM event_publications")), 2)

    def test_event_delete_preserves_tombstones_and_cascades_rsvp(self):
        command.upgrade(self.config, "head")
        self.publication()
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM events WHERE id=1"))
            self.assertEqual(connection.execute(text(
                "SELECT message_id,guild_id,channel_id,event_id FROM event_publications"
            )).all(), [(1000, 1, 101, None)])
            self.assertEqual(connection.scalar(text("SELECT COUNT(*) FROM event_rsvps WHERE event_id=1")), 0)
            self.assertEqual(connection.execute(text("PRAGMA foreign_key_check")).all(), [])

    def test_empty_publication_downgrade_reupgrade_preserves_prior_data(self):
        command.upgrade(self.config, "head")
        command.downgrade(self.config, "d82a19f603b7")
        self.assertEqual(self.snapshot(), self.before)
        command.upgrade(self.config, "head")
        self.assertEqual(self.snapshot(), self.before)
        command.check(self.config)

    def test_migrated_reservation_ids_do_not_reuse_deleted_values(self):
        command.upgrade(self.config, "head")
        self.publication()
        with self.engine.begin() as connection:
            old = connection.scalar(text("SELECT id FROM event_publications"))
            connection.execute(text("DELETE FROM event_publications"))
        self.publication(message=1001)
        with self.engine.connect() as connection:
            self.assertGreater(connection.scalar(text("SELECT id FROM event_publications")), old)

    def test_downgrade_refuses_to_orphan_active_cards(self):
        command.upgrade(self.config, "head")
        self.publication()
        with self.assertRaisesRegex(RuntimeError, "event publications exist"):
            command.downgrade(self.config, "d82a19f603b7")
        self.assertEqual(self.snapshot(), self.before)
        with self.engine.connect() as connection:
            self.assertEqual(connection.scalar(text("SELECT COUNT(*) FROM event_publications")), 1)
            self.assertEqual(connection.scalar(text("SELECT version_num FROM alembic_version")), "e93b20a714c8")
