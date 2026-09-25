"""Migrate the exact pre-RSVP schema with one-time and weekly history."""
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError


class ParticipationMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        url = f"sqlite:///{Path(directory.name) / 'participation.db'}"
        for target, value in (("lastz_bot.database.session.DATABASE_URL", url),
                               ("logging.config.fileConfig", lambda *args, **kwargs: None)):
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

        command.upgrade(self.config, "c41e62b79a10")
        with self.engine.begin() as connection:
            for tenant in (1, 2):
                connection.execute(text("INSERT INTO guilds (id,name) VALUES (:id,'Guild')"), {"id": tenant})
                connection.execute(text(
                    "INSERT INTO alliances (id,guild_id,name,reminder_channel_id) VALUES (:id,:id,'Alpha',:channel)"
                ), {"id": tenant, "channel": tenant * 100})
            connection.execute(text(
                "INSERT INTO event_series (id,alliance_id,name,active,created_by_discord_user_id,created_at) "
                "VALUES (1,1,'Weekly',1,10,'2026-09-01 00:00:00.000000')"
            ))
            connection.execute(text(
                "INSERT INTO weekly_schedules (id,series_id,anchor_at,next_slot_at,name) "
                "VALUES (1,1,'2026-09-18 17:00:00.000000','2026-09-25 17:00:00.000000','Weekly')"
            ))
            for occurrence, alliance in ((1, 1), (2, 2)):
                connection.execute(text(
                    "INSERT INTO events (id,alliance_id,name,starts_at,created_by_discord_user_id) "
                    "VALUES (:id,:alliance,'One time','2026-09-25 19:00:00.000000',10)"
                ), {"id": occurrence, "alliance": alliance})
            for occurrence, day in ((3, 18), (4, 25)):
                connection.execute(text(
                    "INSERT INTO events (id,alliance_id,series_id,schedule_id,nominal_at,name,starts_at,created_by_discord_user_id) "
                    "VALUES (:id,1,1,1,:nominal,'Weekly',:start,10)"
                ), {"id": occurrence, "nominal": f"2026-09-{day} 17:00:00.000000", "start": f"2026-09-{day} 19:00:00.000000"})
            for occurrence, status in ((1, "claimed"), (3, "sent"), (4, "skipped")):
                connection.execute(text(
                    "INSERT INTO event_reminders (event_id,lead_minutes,status,recorded_at,claim_token) "
                    "VALUES (:id,30,:status,'2026-09-18 18:30:00.000000',:token)"
                ), {"id": occurrence, "status": status, "token": f"old-{occurrence}"})
        self.columns = {table: [c["name"] for c in inspect(self.engine).get_columns(table)]
                        for table in ("guilds", "alliances", "events", "event_series", "weekly_schedules", "event_reminders")}
        self.before = self.snapshot()

    def snapshot(self):
        with self.engine.connect() as connection:
            return {table: connection.execute(text(
                f"SELECT {','.join(columns)} FROM {table} ORDER BY {columns[0]}"
            )).all() for table, columns in self.columns.items()}

    def test_upgrade_preserves_existing_rows_and_defaults_all_modes_to_none(self):
        command.upgrade(self.config, "head")
        self.assertEqual(self.snapshot(), self.before)
        with self.engine.connect() as connection:
            for table, count in (("events", 4), ("event_series", 1), ("weekly_schedules", 1)):
                self.assertEqual(connection.execute(text(f"SELECT participation FROM {table}")).all(), [("none",)] * count)
            self.assertEqual(connection.execute(text("SELECT participation_overridden FROM events")).all(), [(0,)] * 4)
            self.assertEqual(connection.scalar(text("SELECT COUNT(*) FROM event_rsvps")), 0)
            self.assertEqual(connection.execute(text("PRAGMA foreign_key_check")).all(), [])
            self.assertEqual(connection.scalar(text("SELECT version_num FROM alembic_version")), "d82a19f603b7")
        command.current(self.config)
        command.check(self.config)
        self.assertIn("d82a19f603b7 (head)", self.config.stdout.getvalue())
        self.assertIn("No new upgrade operations detected", self.config.stdout.getvalue())

    def test_migrated_database_uniqueness_checks_and_cascade(self):
        command.upgrade(self.config, "head")
        sql = text(
            "INSERT INTO event_rsvps (event_id,discord_user_id,response,created_at,updated_at) "
            "VALUES (:event_id,30,:response,'2026-09-25 18:00:00.000000','2026-09-25 18:00:00.000000')"
        )
        with self.engine.begin() as connection:
            connection.execute(sql, {"event_id": 1, "response": "going"})
        for occurrence, response in ((1, "maybe"), (999, "going"), (2, "attended")):
            with self.assertRaises(IntegrityError), self.engine.begin() as connection:
                connection.execute(sql, {"event_id": occurrence, "response": response})
        with self.assertRaises(IntegrityError), self.engine.begin() as connection:
            connection.execute(text("UPDATE events SET participation='invalid'"))
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM events WHERE id=1"))
            self.assertEqual(connection.scalar(text("SELECT COUNT(*) FROM event_rsvps")), 0)
            self.assertEqual(connection.execute(text("PRAGMA foreign_key_check")).all(), [])

    def test_downgrade_reupgrade_preserves_default_one_time_and_weekly_history(self):
        command.upgrade(self.config, "head")
        command.downgrade(self.config, "c41e62b79a10")
        self.assertEqual(self.snapshot(), self.before)
        self.assertNotIn("event_rsvps", inspect(self.engine).get_table_names())
        command.upgrade(self.config, "head")
        self.assertEqual(self.snapshot(), self.before)
        command.check(self.config)

    def test_downgrade_refuses_to_drop_retained_intentions_even_when_none(self):
        command.upgrade(self.config, "head")
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO event_rsvps VALUES (1,30,'going','2026-09-25 18:00:00.000000','2026-09-25 18:00:00.000000')"
            ))
        with self.assertRaisesRegex(RuntimeError, "RSVP or participation data"):
            command.downgrade(self.config, "c41e62b79a10")
        self.assertEqual(self.snapshot(), self.before)
        with self.engine.connect() as connection:
            self.assertEqual(connection.scalar(text("SELECT COUNT(*) FROM event_rsvps")), 1)
            self.assertEqual(connection.scalar(text("SELECT version_num FROM alembic_version")), "d82a19f603b7")

    def test_downgrade_refuses_to_erase_participation_configuration(self):
        command.upgrade(self.config, "head")
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE event_series SET participation='required'"))
        with self.assertRaisesRegex(RuntimeError, "RSVP or participation data"):
            command.downgrade(self.config, "c41e62b79a10")
        with self.engine.connect() as connection:
            self.assertEqual(connection.scalar(text("SELECT participation FROM event_series")), "required")
