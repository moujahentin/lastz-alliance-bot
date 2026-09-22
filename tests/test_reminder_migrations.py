from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError


class ReminderMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        url = f"sqlite:///{Path(directory.name) / 'migration.db'}"
        patcher = patch("lastz_bot.database.session.DATABASE_URL", url)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Alembic's CLI logging setup disables application loggers globally.
        # Keep migration tests isolated from the rest of unittest discovery.
        logging_patch = patch("logging.config.fileConfig")
        logging_patch.start()
        self.addCleanup(logging_patch.stop)
        root = Path(__file__).resolve().parents[1]
        self.config = Config(str(root / "alembic.ini"), stdout=StringIO())
        self.config.set_main_option("script_location", str(root / "migrations"))
        self.head = ScriptDirectory.from_config(self.config).get_current_head()
        self.engine = create_engine(url)
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")

        command.upgrade(self.config, "e84012607d93")
        with self.engine.begin() as connection:
            connection.execute(text("INSERT INTO guilds (id, name) VALUES (1, 'One')"))
            connection.execute(text("INSERT INTO alliances (id, guild_id, name) VALUES (1, 1, 'Alpha')"))
            connection.execute(text(
                "INSERT INTO events (id, alliance_id, name, starts_at, created_by_discord_user_id) "
                "VALUES (1, 1, 'Duel', '2026-09-25 19:00:00.000000', 10)"
            ))
        command.upgrade(self.config, "head")

    def insert_reminder(self, event_id=1, lead=30, status="claimed"):
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO event_reminders (event_id, lead_minutes, status, recorded_at) "
                "VALUES (:event_id, :lead, :status, '2026-09-25 18:30:00.000000')"
            ), {"event_id": event_id, "lead": lead, "status": status})

    def test_upgrade_preserves_existing_events_and_matches_models(self):
        with self.engine.connect() as connection:
            self.assertIsNone(connection.scalar(text("SELECT reminder_channel_id FROM alliances")))
            self.assertEqual(connection.scalar(text("SELECT starts_at FROM events")), "2026-09-25 19:00:00.000000")
            self.assertEqual(connection.scalar(text("SELECT COUNT(*) FROM event_reminders")), 0)
            self.assertEqual(connection.scalar(text("SELECT version_num FROM alembic_version")), self.head)
        command.current(self.config)
        command.check(self.config)
        self.assertIn(f"{self.head} (head)", self.config.stdout.getvalue())
        self.assertIn("No new upgrade operations detected", self.config.stdout.getvalue())

    def test_constraints_enforce_two_unique_opportunities_and_valid_status(self):
        self.insert_reminder(lead=30)
        self.insert_reminder(lead=10, status="skipped")
        for event_id, lead, status in ((1, 30, "sent"), (1, 15, "claimed"), (999, 30, "claimed")):
            with self.subTest(event_id=event_id, lead=lead, status=status), self.assertRaises(IntegrityError):
                self.insert_reminder(event_id, lead, status)
        with self.assertRaises(IntegrityError), self.engine.begin() as connection:
            connection.execute(text("UPDATE event_reminders SET status = 'pending'"))

    def test_event_delete_cascades_reminder_state(self):
        self.insert_reminder()
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM events WHERE id = 1"))
            self.assertEqual(connection.scalar(text("SELECT COUNT(*) FROM event_reminders")), 0)

    def test_alliance_delete_cascades_events_and_reminders(self):
        self.insert_reminder()
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM alliances WHERE id = 1"))
            self.assertEqual(connection.scalar(text("SELECT COUNT(*) FROM events")), 0)
            self.assertEqual(connection.scalar(text("SELECT COUNT(*) FROM event_reminders")), 0)

    def test_downgrade_and_reupgrade_preserve_event_data(self):
        self.insert_reminder()
        command.downgrade(self.config, "e84012607d93")
        self.assertNotIn("event_reminders", inspect(self.engine).get_table_names())
        self.assertNotIn("reminder_channel_id", {c["name"] for c in inspect(self.engine).get_columns("alliances")})
        with self.engine.connect() as connection:
            self.assertEqual(connection.scalar(text("SELECT starts_at FROM events")), "2026-09-25 19:00:00.000000")
        command.upgrade(self.config, "head")
        command.check(self.config)

    def test_claim_token_upgrade_preserves_legacy_terminal_records(self):
        command.downgrade(self.config, "72c03cfe92ad")
        self.insert_reminder(lead=30, status="claimed")
        self.insert_reminder(lead=10, status="sent")
        command.upgrade(self.config, "head")
        with self.engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT lead_minutes, status, claim_token FROM event_reminders ORDER BY lead_minutes"
            )).all()
        self.assertEqual(rows, [(10, "sent", None), (30, "claimed", None)])
        command.check(self.config)
        command.downgrade(self.config, "72c03cfe92ad")
        self.assertNotIn("claim_token", {c["name"] for c in inspect(self.engine).get_columns("event_reminders")})
        with self.engine.connect() as connection:
            self.assertEqual(connection.scalar(text("SELECT COUNT(*) FROM event_reminders")), 2)
