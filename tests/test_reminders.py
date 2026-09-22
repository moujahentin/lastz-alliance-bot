import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock

from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from lastz_bot.database.base import Base
from lastz_bot.database.models import Alliance, Event, EventReminder, Guild
from lastz_bot.event_time import parse_apocalypse_time
from lastz_bot.reminders import ReminderProcessor, eligible_threshold


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.url = f"sqlite:///{Path(directory.name) / 'reminders.db'}"
        self.connect()
        Base.metadata.create_all(self.engine)
        self.starts_at = parse_apocalypse_time("2026-09-25 17:00")
        self.now = self.starts_at - timedelta(minutes=30)
        self.send = AsyncMock()
        with self.sessions() as session:
            session.add_all([Guild(id=1, name="One"), Guild(id=2, name="Two")])
            session.flush()
            session.add_all([
                Alliance(id=1, guild_id=1, name="Alpha", reminder_channel_id=101),
                Alliance(id=2, guild_id=1, name="Bravo", reminder_channel_id=102),
                Alliance(id=3, guild_id=2, name="Alpha", reminder_channel_id=201),
            ])
            session.commit()
        self.add_event(1)

    def connect(self):
        self.engine = create_engine(self.url)
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")

        self.sessions = sessionmaker(bind=self.engine)

    def processor(self):
        return ReminderProcessor(self.sessions, self.send, lambda: self.now)

    def add_event(self, alliance_id):
        with self.sessions() as session:
            record = Event(
                alliance_id=alliance_id, name=f"Duel {alliance_id}",
                starts_at=self.starts_at, created_by_discord_user_id=10,
            )
            session.add(record)
            session.commit()
            return record.id

    def states(self, event_id=1):
        with self.sessions() as session:
            return {
                row.lead_minutes: row.status for row in session.scalars(
                    select(EventReminder).where(EventReminder.event_id == event_id)
                )
            }

    def set_channel(self, channel_id):
        with self.sessions() as session:
            session.get(Alliance, 1).reminder_channel_id = channel_id
            session.commit()

    async def test_30_minute_eligibility_and_persisted_success(self):
        await self.processor().process_pending()
        self.send.assert_awaited_once()
        delivery = self.send.call_args.args[0]
        self.assertEqual(delivery.lead_minutes, 30)
        self.assertEqual(delivery.starts_at, datetime(2026, 9, 25, 19))
        self.assertIsNone(delivery.starts_at.tzinfo)
        self.assertEqual(self.states(), {30: "sent"})
        with self.sessions() as session:
            record = session.get(EventReminder, (1, 30))
            self.assertEqual(record.channel_id, 101)
            self.assertEqual(record.recorded_at, self.now)
            self.assertEqual(record.sent_at, self.now)
            self.assertIsNone(record.sent_at.tzinfo)

    async def test_no_early_reminder(self):
        self.now -= timedelta(microseconds=1)
        await self.processor().process_pending()
        self.send.assert_not_awaited()
        self.assertEqual(self.states(), {})

    async def test_10_minute_reminder_after_30_was_sent(self):
        await self.processor().process_pending()
        self.now = self.starts_at - timedelta(minutes=10, microseconds=1)
        await self.processor().process_pending()
        self.assertEqual(self.send.await_count, 1)
        self.now += timedelta(microseconds=1)
        await self.processor().process_pending()
        self.assertEqual([c.args[0].lead_minutes for c in self.send.await_args_list], [30, 10])
        self.assertEqual(self.states(), {30: "sent", 10: "sent"})

    async def test_restart_and_reprocessing_do_not_repeat_sent_reminder(self):
        await self.processor().process_pending()
        self.engine.dispose()
        self.connect()  # A new engine and processor read the same persistent DB.
        await self.processor().process_pending()
        self.send.assert_awaited_once()
        self.now = self.starts_at - timedelta(minutes=10)
        await self.processor().process_pending()
        await self.processor().process_pending()
        self.assertEqual(self.send.await_count, 2)

    async def test_late_event_or_restart_only_sends_latest_and_persists_skip(self):
        self.now = self.starts_at - timedelta(minutes=5)
        await self.processor().process_pending()
        self.send.assert_awaited_once()
        self.assertEqual(self.send.call_args.args[0].lead_minutes, 10)
        self.assertEqual(self.states(), {30: "skipped", 10: "sent"})
        self.engine.dispose()
        self.connect()
        await self.processor().process_pending()
        # Even a backwards clock adjustment cannot revive the older reminder.
        self.now = self.starts_at - timedelta(minutes=20)
        await self.processor().process_pending()
        self.send.assert_awaited_once()

    async def test_no_reminder_at_or_after_start(self):
        for elapsed in (timedelta(0), timedelta(seconds=1), timedelta(days=1)):
            self.now = self.starts_at + elapsed
            await self.processor().process_pending()
        self.send.assert_not_awaited()
        self.assertEqual(self.states(), {})

    async def test_missing_channel_then_late_configuration(self):
        self.set_channel(None)
        await self.processor().process_pending()
        self.assertEqual(self.states(), {})
        self.now = self.starts_at - timedelta(minutes=5)
        await self.processor().process_pending()
        self.assertEqual(self.states(), {30: "skipped"})
        self.send.assert_not_awaited()
        self.set_channel(103)
        await self.processor().process_pending()
        self.send.assert_awaited_once()
        self.assertEqual(self.send.call_args.args[0].channel_id, 103)
        self.assertEqual(self.send.call_args.args[0].lead_minutes, 10)

    async def test_crash_after_claim_before_send_never_retries(self):
        delivery = self.processor().claim(1)
        self.assertIsNotNone(delivery)
        self.assertEqual(self.states(), {30: "claimed"})
        self.engine.dispose()
        self.connect()
        await self.processor().process_pending()
        self.send.assert_not_awaited()

    async def test_claim_is_visible_in_database_before_sender_is_called(self):
        async def inspect_claim(delivery):
            self.assertEqual(self.states(), {30: "claimed"})
            # A second processor cannot acquire the opportunity during the send.
            self.assertIsNone(self.processor().claim(delivery.event_id))

        self.send.side_effect = inspect_claim
        await self.processor().process_pending()
        self.assertEqual(self.states(), {30: "sent"})

    async def test_failure_recording_success_preserves_claim_after_restart(self):
        def fail_success_update(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE event_reminders"):
                raise OperationalError("UPDATE event_reminders", {}, RuntimeError("disk failure"))

        event.listen(self.engine, "before_cursor_execute", fail_success_update)
        with self.assertRaises(OperationalError):
            await self.processor().process_pending()
        self.assertEqual(self.states(), {30: "claimed"})
        self.engine.dispose()
        self.connect()
        await self.processor().process_pending()
        self.send.assert_awaited_once()

    async def test_crash_after_discord_send_before_success_record_never_retries(self):
        async def accepted_then_crashed(delivery):
            # Discord accepted the message, but the process dies before success.
            raise asyncio.CancelledError()

        self.send.side_effect = accepted_then_crashed
        with self.assertRaises(asyncio.CancelledError):
            await self.processor().process_pending()
        self.assertEqual(self.states(), {30: "claimed"})
        self.engine.dispose()
        self.connect()
        self.send.side_effect = None
        await self.processor().process_pending()
        self.send.assert_awaited_once()

    async def test_send_failure_keeps_claim_and_does_not_block_other_alliances(self):
        other_id = self.add_event(2)
        self.send.side_effect = [RuntimeError("uncertain send"), None]
        with self.assertLogs("lastz_bot.reminders", level="WARNING"):
            await self.processor().process_pending()
        self.assertEqual(self.states(), {30: "claimed"})
        self.assertEqual(self.states(other_id), {30: "sent"})
        await self.processor().process_pending()
        self.assertEqual(self.send.await_count, 2)

    async def test_channels_and_state_are_isolated_across_alliances_and_guilds(self):
        bravo = self.add_event(2)
        foreign_alpha = self.add_event(3)
        # One alliance's existing claim must not suppress another's opportunity.
        self.processor().claim(1)
        await self.processor().process_pending()
        self.assertEqual(
            [(c.args[0].event_id, c.args[0].guild_id, c.args[0].alliance_id, c.args[0].channel_id)
             for c in self.send.await_args_list],
            [(bravo, 1, 2, 102), (foreign_alpha, 2, 3, 201)],
        )
        self.assertEqual(self.states(), {30: "claimed"})
        self.assertEqual(self.states(bravo), {30: "sent"})
        self.assertEqual(self.states(foreign_alpha), {30: "sent"})

    async def test_channel_change_does_not_repeat_old_threshold(self):
        await self.processor().process_pending()
        self.set_channel(104)
        await self.processor().process_pending()
        self.send.assert_awaited_once()
        self.now = self.starts_at - timedelta(minutes=10)
        await self.processor().process_pending()
        self.assertEqual([c.args[0].channel_id for c in self.send.await_args_list], [101, 104])

    async def test_time_is_rechecked_after_a_slow_prior_send(self):
        self.add_event(2)

        async def slow_send(delivery):
            self.now = self.starts_at

        self.send.side_effect = slow_send
        await self.processor().process_pending()
        self.send.assert_awaited_once()
        self.assertEqual(self.states(2), {})

    def test_concurrent_processors_only_one_can_claim(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.processor().claim(1), range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.states(), {30: "claimed"})

    def test_threshold_boundaries(self):
        for remaining, expected in ((1801, None), (1800, 30), (601, 30), (600, 10), (1, 10), (0, None), (-1, None)):
            with self.subTest(remaining=remaining):
                self.assertEqual(
                    eligible_threshold(self.starts_at, self.starts_at - timedelta(seconds=remaining)),
                    expected,
                )
