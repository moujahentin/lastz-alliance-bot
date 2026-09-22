"""Upgrade real preceding-schema rows, including all terminal delivery states."""
from datetime import datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

from lastz_bot.database.models import Alliance
from lastz_bot.recurrence import create_weekly


class WeeklyMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        url = f"sqlite:///{Path(directory.name) / 'previous-schema.db'}"
        for target, value in [("lastz_bot.database.session.DATABASE_URL", url),
                              ("logging.config.fileConfig", lambda *a, **kw: None)]:
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        root = Path(__file__).resolve().parents[1]
        self.config = Config(str(root / "alembic.ini"), stdout=StringIO())
        self.config.set_main_option("script_location", str(root / "migrations"))
        self.engine = create_engine(url)
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")

        command.upgrade(self.config, "9fd174f83e21")
        with self.engine.begin() as connection:
            for tenant in (1, 2):
                connection.execute(text("INSERT INTO guilds (id,name) VALUES (:id,'Guild')"), {"id": tenant})
                connection.execute(text(
                    "INSERT INTO alliances (id,guild_id,name,reminder_channel_id) VALUES (:id,:id,'Alpha',:channel)"
                ), {"id": tenant, "channel": tenant * 100})
                for number, status in enumerate(("claimed", "sent", "skipped"), 1):
                    occurrence_id = tenant * 100 + number
                    connection.execute(text(
                        "INSERT INTO events (id,alliance_id,name,description,starts_at,created_by_discord_user_id,created_at) "
                        "VALUES (:id,:tenant,'Legacy','Preserve','2026-09-22 19:00:00.000000',10,'2026-09-01 12:00:00.000000')"
                    ), {"id": occurrence_id, "tenant": tenant})
                    connection.execute(text(
                        "INSERT INTO event_reminders "
                        "(event_id,lead_minutes,status,claim_token,channel_id,recorded_at,sent_at) "
                        "VALUES (:id,30,:status,:token,:channel,'2026-09-22 18:30:00.000000',:sent)"
                    ), {"id": occurrence_id, "status": status,
                        "token": None if status == "skipped" else str(occurrence_id),
                        "channel": None if status == "skipped" else tenant * 100,
                        "sent": "2026-09-22 18:30:01.000000" if status == "sent" else None})
        self.before = self.snapshot()

    def snapshot(self):
        with self.engine.connect() as connection:
            return (
                connection.execute(text(
                    "SELECT id,alliance_id,name,description,starts_at,created_by_discord_user_id,created_at "
                    "FROM events ORDER BY id"
                )).all(),
                connection.execute(text("SELECT * FROM event_reminders ORDER BY event_id,lead_minutes")).all(),
                connection.execute(text("SELECT * FROM alliances ORDER BY id")).all(),
            )

    def test_upgrade_preserves_exact_legacy_ids_times_channels_and_claims(self):
        command.upgrade(self.config, "head")
        self.assertEqual(self.snapshot(), self.before)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text(
                "SELECT series_id,schedule_id,nominal_at,status,is_exception FROM events"
            )).all(), [(None, None, None, "scheduled", 0)] * 6)
            self.assertEqual(connection.execute(text("PRAGMA foreign_key_check")).all(), [])
            self.assertEqual(connection.scalar(text("SELECT version_num FROM alembic_version")), "c41e62b79a10")
        command.current(self.config)
        command.check(self.config)
        self.assertIn("c41e62b79a10 (head)", self.config.stdout.getvalue())
        self.assertIn("No new upgrade operations detected", self.config.stdout.getvalue())

    def test_downgrade_and_reupgrade_preserve_all_existing_data(self):
        command.upgrade(self.config, "head")
        command.downgrade(self.config, "9fd174f83e21")
        self.assertEqual(self.snapshot(), self.before)
        self.assertNotIn("event_series", inspect(self.engine).get_table_names())
        command.upgrade(self.config, "head")
        self.assertEqual(self.snapshot(), self.before)
        command.check(self.config)

    def test_downgrade_refuses_to_discard_weekly_history(self):
        command.upgrade(self.config, "head")
        sessions = sessionmaker(bind=self.engine)
        with sessions() as session:
            create_weekly(session, session.get(Alliance, 1), "Weekly", None,
                          datetime(2026, 9, 22, 19), 10, datetime(2026, 9, 22, 18))
            session.commit()
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "weekly series/history exist"):
            command.downgrade(self.config, "9fd174f83e21")
        self.assertEqual(self.snapshot(), before)
        with self.engine.connect() as connection:
            self.assertEqual(connection.scalar(text("SELECT version_num FROM alembic_version")), "c41e62b79a10")
            self.assertEqual(connection.scalar(text("SELECT COUNT(*) FROM event_series")), 1)
            self.assertEqual(connection.execute(text("PRAGMA foreign_key_check")).all(), [])
