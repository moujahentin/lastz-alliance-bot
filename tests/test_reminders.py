import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from lastz_bot.database.base import Base
from lastz_bot.database.models import Alliance, Event, EventReminder, Guild
from lastz_bot.event_time import parse_apocalypse_time
from lastz_bot.event_management import delete_event, edit_event
from lastz_bot.reminders import ReminderProcessor, eligible_threshold
from lastz_bot.reminder_worker import ReminderWorker


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

        self.sessions = sessionmaker(bind=self.engine, autoflush=False)

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

    def reschedule(self, starts_at):
        return edit_event(self.sessions, 1, 1, 10, True, starts_at=starts_at)

    async def test_reschedule_rebuilds_both_opportunities_after_restart(self):
        await self.processor().process_pending()
        self.now = self.starts_at - timedelta(minutes=10)
        await self.processor().process_pending()
        self.assertEqual(self.states(), {30: "sent", 10: "sent"})
        self.reschedule("2026-09-26 17:00")
        self.assertEqual(self.states(), {})
        self.engine.dispose()
        self.connect()
        self.now = datetime(2026, 9, 26, 18, 30)
        await self.processor().process_pending()
        self.now = datetime(2026, 9, 26, 18, 50)
        await self.processor().process_pending()
        self.assertEqual([c.args[0].lead_minutes for c in self.send.await_args_list], [30, 10, 30, 10])
        self.assertEqual(self.states(), {30: "sent", 10: "sent"})

    async def test_reschedule_resets_skipped_and_claimed_state_with_latest_only_catchup(self):
        self.now = self.starts_at - timedelta(minutes=5)
        self.processor().claim(1)
        self.assertEqual(self.states(), {30: "skipped", 10: "claimed"})
        self.reschedule("2026-09-25 17:01")
        self.assertEqual(self.states(), {})
        await self.processor().process_pending()
        self.send.assert_awaited_once()
        self.assertEqual(self.send.call_args.args[0].lead_minutes, 10)
        self.assertEqual(self.send.call_args.args[0].starts_at, datetime(2026, 9, 25, 19, 1))

    async def test_reschedule_to_past_never_sends(self):
        self.reschedule("2026-09-24 17:00")
        await self.processor().process_pending()
        self.send.assert_not_awaited()

    def test_old_claim_invalid_after_reschedule_away_and_back_even_with_same_clock(self):
        processor = self.processor()
        old = processor.claim(1)
        self.reschedule("2026-09-25 16:59")
        self.reschedule("2026-09-25 17:00")
        new = processor.claim(1)
        self.assertNotEqual(old.claim_token, new.claim_token)
        self.assertIsNone(processor.current_delivery(old))
        self.assertEqual(processor.current_delivery(new), new)
        processor.mark_sent(old)
        self.assertEqual(self.states(), {30: "claimed"})
        processor.mark_sent(new)
        self.assertEqual(self.states(), {30: "sent"})

    async def test_inflight_old_send_does_not_complete_new_claim_after_edit(self):
        async def reschedule_during_send(delivery):
            self.reschedule("2026-09-25 16:59")
            new = self.processor().claim(1)
            self.assertNotEqual(new.claim_token, delivery.claim_token)

        self.send.side_effect = reschedule_during_send
        await self.processor().process_pending()
        self.assertEqual(self.states(), {30: "claimed"})
        with self.sessions() as session:
            self.assertIsNone(session.get(EventReminder, (1, 30)).sent_at)

    async def test_inflight_send_after_delete_cannot_recreate_reminder_state(self):
        async def delete_during_send(delivery):
            delete_event(self.sessions, 1, 1, 10, True)

        self.send.side_effect = delete_during_send
        await self.processor().process_pending()
        self.assertEqual(self.states(), {})
        with self.sessions() as session:
            self.assertIsNone(session.get(Event, 1))

    def test_deleted_event_id_reuse_does_not_revive_old_claim(self):
        processor = self.processor()
        old = processor.claim(1)
        delete_event(self.sessions, 1, 1, 10, True)
        self.assertEqual(self.add_event(1), 1)  # SQLite can reuse the highest deleted ID.
        new = processor.claim(1)
        self.assertIsNone(processor.current_delivery(old))
        processor.mark_sent(old)
        self.assertEqual(self.states(), {30: "claimed"})
        self.assertNotEqual(old.claim_token, new.claim_token)

    def test_metadata_edit_keeps_claim_and_refreshes_reminder_name(self):
        processor = self.processor()
        original = processor.claim(1)
        edit_event(self.sessions, 1, 1, 10, True, name="Renamed", description="Details")
        current = processor.current_delivery(original)
        self.assertEqual(current.event_name, "Renamed")
        self.assertEqual(current.claim_token, original.claim_token)
        self.assertEqual(self.states(), {30: "claimed"})
        processor.mark_sent(original)
        self.assertEqual(self.states(), {30: "sent"})

    def test_edit_failure_rolls_back_time_and_reminder_reset_together(self):
        self.processor().claim(1)
        statements = []

        def fail_update(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)
            if statement.startswith("UPDATE events"):
                raise OperationalError("UPDATE events", {}, RuntimeError("disk failure"))

        event.listen(self.engine, "before_cursor_execute", fail_update)
        with self.assertRaises(OperationalError):
            self.reschedule("2026-09-26 17:00")
        self.assertTrue(any(sql.startswith("DELETE FROM event_reminders") for sql in statements))
        self.assertEqual(self.states(), {30: "claimed"})
        with self.sessions() as session:
            self.assertEqual(session.get(Event, 1).starts_at, self.starts_at)

    def test_concurrent_claim_and_reschedule_leave_only_current_claims(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            claim = pool.submit(self.processor().claim, 1)
            change = pool.submit(self.reschedule, "2026-09-25 16:59")
            delivery = claim.result()
            change.result()
        with self.sessions() as session:
            self.assertEqual(session.get(Event, 1).starts_at, datetime(2026, 9, 25, 18, 59))
        if delivery.starts_at == self.starts_at:
            self.assertIsNone(self.processor().current_delivery(delivery))
            self.assertEqual(self.states(), {})
        else:
            self.assertEqual(self.processor().current_delivery(delivery), delivery)
            self.assertEqual(self.states(), {30: "claimed"})

    def test_concurrent_claim_and_delete_leave_no_orphan_state(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            claim = pool.submit(self.processor().claim, 1)
            removal = pool.submit(delete_event, self.sessions, 1, 1, 10, True)
            delivery = claim.result()
            removal.result()
        self.assertEqual(self.states(), {})
        if delivery is not None:
            self.assertIsNone(self.processor().current_delivery(delivery))
            self.processor().mark_sent(delivery)
            self.assertEqual(self.states(), {})

    def discord_worker(self):
        client = Mock()
        channel = Mock(spec=discord.TextChannel)
        channel.guild = SimpleNamespace(id=1)
        channel.send = AsyncMock()
        client.get_guild.return_value.get_channel.return_value = channel
        worker = ReminderWorker(client)
        worker.processor = self.processor()
        return worker, channel

    async def test_discord_send_rechecks_claim_after_reschedule(self):
        worker, channel = self.discord_worker()
        delivery = worker.processor.claim(1)
        self.reschedule("2026-09-25 16:59")
        with self.assertRaisesRegex(RuntimeError, "deleted or reset"):
            await worker.send(delivery)
        channel.send.assert_not_awaited()

    async def test_discord_send_rechecks_claim_after_delete(self):
        worker, channel = self.discord_worker()
        delivery = worker.processor.claim(1)
        delete_event(self.sessions, 1, 1, 10, True)
        with self.assertRaisesRegex(RuntimeError, "deleted or reset"):
            await worker.send(delivery)
        channel.send.assert_not_awaited()

    async def test_discord_send_uses_latest_name_without_resetting_claim(self):
        worker, channel = self.discord_worker()
        delivery = worker.processor.claim(1)
        edit_event(self.sessions, 1, 1, 10, True, name="Renamed")
        with patch("lastz_bot.reminder_worker.utc_now_naive", return_value=self.now):
            await worker.send(delivery)
        channel.send.assert_awaited_once()
        self.assertIn("**Renamed**", channel.send.call_args.args[0])
        self.assertEqual(self.states(), {30: "claimed"})
