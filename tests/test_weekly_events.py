from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from lastz_bot.commands.event import setup_event_commands
from lastz_bot.database.base import Base
from lastz_bot.database.models import Alliance, Event, EventOccurrence, EventReminder, EventSeries, Guild, Member, WeeklySchedule
from lastz_bot.event_management import EventManagementError, delete_event, edit_event
from lastz_bot.event_time import parse_apocalypse_time
from lastz_bot.recurrence import BACKFILL_BATCH_SIZE, WEEK, create_weekly, edit_series, ensure_occurrences, next_weekly_slot, slot_utc, stop_series
from lastz_bot.reminders import ReminderProcessor


class WeeklyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.url = f"sqlite:///{Path(directory.name) / 'weekly.db'}"
        self.connect()
        Base.metadata.create_all(self.engine)
        self.now = datetime(2026, 9, 22, 18, 30)
        self.start = datetime(2026, 9, 22, 19)
        self.send = AsyncMock()
        with self.sessions() as session:
            session.add_all([Guild(id=1, name="One"), Guild(id=2, name="Two")])
            session.flush()
            session.add_all([
                Alliance(id=1, guild_id=1, name="Alpha", reminder_channel_id=101),
                Alliance(id=2, guild_id=1, name="Bravo", reminder_channel_id=102),
                Alliance(id=3, guild_id=2, name="Alpha", reminder_channel_id=201),
            ])
            session.flush()
            for alliance, actor, rank in [(1, 10, "R4"), (1, 20, "R5"), (1, 30, "MEMBER"),
                                           (2, 40, "R5"), (3, 50, "R5"), (1, 50, "MEMBER")]:
                session.add(Member(alliance_id=alliance, game_name=str(actor),
                                   discord_user_id=actor, rank=rank))
            session.commit()
        for target, value in [
            ("lastz_bot.commands.event.SessionLocal", self.sessions),
            ("lastz_bot.permissions.SessionLocal", self.sessions),
        ]:
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target in ["lastz_bot.commands.event.utc_now_naive", "lastz_bot.recurrence.utc_now_naive"]:
            patcher = patch(target, side_effect=lambda: self.now)
            patcher.start()
            self.addCleanup(patcher.stop)
        tree = Mock()
        setup_event_commands(tree)
        group = tree.add_command.call_args.args[0]
        self.commands = {c.name: c.callback for c in group.commands}

    def connect(self):
        self.engine = create_engine(self.url)
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")

        self.sessions = sessionmaker(bind=self.engine, autoflush=False)

    def create(self, alliance_id=1, start=None):
        with self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            series = create_weekly(session, session.get(Alliance, alliance_id), "Duel", "Prepare",
                                   start or self.start, 10, self.now)
            series_id = series.id
            session.commit()
            return series_id

    def rows(self, series_id=None):
        with self.sessions() as session:
            query = select(Event).order_by(Event.starts_at, Event.id)
            if series_id is not None:
                query = query.where(Event.series_id == series_id)
            return session.scalars(query).all()

    def processor(self):
        return ReminderProcessor(self.sessions, self.send, lambda: self.now)

    def edit(self, series_id, **kwargs):
        edit_series(self.sessions, 1, series_id, 10, False, now=self.now, **kwargs)

    def stop(self, series_id):
        stop_series(self.sessions, 1, series_id, 10, False, now=self.now)

    def interaction(self, actor=10, guild=1, admin=False):
        return SimpleNamespace(
            guild=SimpleNamespace(id=guild) if guild is not None else None,
            user=SimpleNamespace(id=actor, guild_permissions=SimpleNamespace(administrator=admin)),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

    async def test_create_defaults_to_once_and_explicit_once(self):
        for extra in ({}, {"recurrence": "once"}):
            interaction = self.interaction()
            await self.commands["create"](interaction, "Alpha", "Once", "2026-09-22 17:00", **extra)
            self.assertIn("Event `Once` created", interaction.response.send_message.call_args.args[0])
        self.assertIs(Event, EventOccurrence)
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(e.series_id is None and e.starts_at == self.start for e in self.rows()))

    async def test_create_weekly_command_at_rule_and_utc_occurrence(self):
        interaction = self.interaction()
        await self.commands["create"](interaction, "Alpha", "Duel", "2026-09-22 17:00", recurrence="weekly")
        row = self.rows()[0]
        self.assertEqual(row.starts_at, self.start)
        self.assertIsNone(row.starts_at.tzinfo)
        self.assertEqual(row.nominal_at, datetime(2026, 9, 22, 17))
        self.assertEqual(row.status, "scheduled")
        with self.sessions() as session:
            self.assertEqual(session.get(WeeklySchedule, row.schedule_id).anchor_at, row.nominal_at)
        self.assertIn(f"Weekly series `{row.series_id}`", interaction.response.send_message.call_args.args[0])

    async def test_command_past_weekly_anchor_still_backfills_in_batches(self):
        interaction = self.interaction()
        first_start = self.start - 121 * WEEK
        first_at = (first_start - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
        await self.commands["create"](interaction, "Alpha", "History", first_at, recurrence="weekly")
        self.assertIn("Weekly series", interaction.response.send_message.call_args.args[0])
        self.assertEqual(len(self.rows()), 51)
        self.assertEqual(self.rows()[0].starts_at, first_start)
        self.assertEqual(ensure_occurrences(self.sessions, self.now), 50)
        self.assertEqual(ensure_occurrences(self.sessions, self.now), 21)
        self.assertEqual(len(self.rows()), 122)
        self.assertTrue(all(row.status == "scheduled" for row in self.rows()))
        ids = [row.id for row in self.rows()]
        ensure_occurrences(self.sessions, self.now)
        self.assertEqual([row.id for row in self.rows()], ids)

    async def test_list_concrete_ids_weekly_badge_order_and_isolation(self):
        first = self.create()
        self.create(2)
        self.create(3)
        interaction = self.interaction()
        await self.commands["create"](interaction, "Alpha", "Earlier", "2026-09-22 16:45")
        listing = self.interaction()
        await self.commands["list"](listing, "Alpha")
        message = listing.response.send_message.call_args.args[0]
        self.assertEqual(message.count("Weekly"), 1)
        self.assertIn(f"Series ID `{first}`", message)
        self.assertIn(f"Occurrence ID `{self.rows(first)[0].id}`", message)
        self.assertIn("17:00` AT", message)
        self.assertLess(message.index("Earlier"), message.index("Duel"))

    async def test_list_generates_next_occurrence_for_only_requested_tenant(self):
        series = self.create()
        foreign = self.create(3)
        self.now = self.start + WEEK
        await self.commands["list"](self.interaction(), "Alpha")
        self.assertEqual(len(self.rows(series)), 3)
        self.assertEqual(len(self.rows(foreign)), 1)

    def test_weekday_midnight_and_year_rollover(self):
        for anchor, now, expected in [
            (datetime(2026, 9, 27, 23, 30), datetime(2026, 9, 28, 1, 30), datetime(2026, 10, 4, 23, 30)),
            (datetime(2026, 9, 28), datetime(2026, 9, 28, 1, 59), datetime(2026, 9, 28)),
            (datetime(2026, 12, 29, 17), datetime(2026, 12, 29, 19), datetime(2027, 1, 5, 17)),
        ]:
            with self.subTest(anchor=anchor):
                self.assertEqual(next_weekly_slot(anchor, now), expected)
                self.assertEqual(slot_utc(expected), expected + timedelta(hours=2))

    def test_future_start_date_is_not_backfilled_before_anchor(self):
        series = self.create(start=self.start + 12 * WEEK)
        ensure_occurrences(self.sessions, self.now)
        self.assertEqual([e.starts_at for e in self.rows(series)], [self.start + 12 * WEEK])

    def test_past_start_backfills_bounded_batches_and_one_live_future(self):
        series = self.create(start=self.start - 121 * WEEK)
        self.assertEqual(len(self.rows(series)), BACKFILL_BATCH_SIZE + 1)
        self.assertEqual(sum(e.starts_at > self.now for e in self.rows(series)), 1)
        self.assertEqual(ensure_occurrences(self.sessions, self.now), 50)
        self.assertEqual(len(self.rows(series)), 101)
        self.assertEqual(ensure_occurrences(self.sessions, self.now), 21)
        rows = self.rows(series)
        self.assertEqual(len(rows), 122)
        self.assertEqual(len({(e.schedule_id, e.nominal_at) for e in rows}), 122)
        self.assertTrue(all(e.status == "scheduled" for e in rows))
        self.assertEqual(ensure_occurrences(self.sessions, self.now), 0)

    def test_downtime_backfill_continues_after_restart_with_stable_ids(self):
        series = self.create()
        first_id = self.rows(series)[0].id
        self.now = self.start + 120 * WEEK
        ensure_occurrences(self.sessions, self.now)
        before = [(e.id, e.starts_at) for e in self.rows(series)]
        self.engine.dispose()
        self.connect()
        ensure_occurrences(self.sessions, self.now)
        ensure_occurrences(self.sessions, self.now)
        rows = self.rows(series)
        self.assertEqual(len(rows), 122)
        self.assertEqual(rows[0].id, first_id)
        self.assertTrue(set(before).issubset({(e.id, e.starts_at) for e in rows}))
        ids = [e.id for e in rows]
        ensure_occurrences(self.sessions, self.now)
        self.assertEqual([e.id for e in self.rows(series)], ids)

    def test_global_history_budget_and_scoped_generation(self):
        one = self.create()
        two = self.create(3)
        self.now = self.start + 100 * WEEK
        before = len(self.rows())
        self.assertEqual(ensure_occurrences(self.sessions, self.now), 50)
        self.assertLessEqual(len(self.rows()) - before, 52)
        self.assertEqual(sum(e.starts_at > self.now for e in self.rows()), 2)
        foreign_before = [(e.id, e.starts_at) for e in self.rows(two)]
        ensure_occurrences(self.sessions, self.now, guild_id=1, alliance_name="Alpha")
        self.assertEqual([(e.id, e.starts_at) for e in self.rows(two)], foreign_before)
        self.assertGreater(len(self.rows(one)), 1)
        with self.assertRaises(ValueError):
            ensure_occurrences(self.sessions, self.now, guild_id=1)

    async def test_each_occurrence_has_independent_30_and_10_reminders(self):
        series = self.create()
        await self.processor().process_pending()
        self.now = self.start - timedelta(minutes=10)
        await self.processor().process_pending()
        self.now = self.start + WEEK - timedelta(minutes=30)
        await self.processor().process_pending()
        self.now = self.start + WEEK - timedelta(minutes=10)
        await self.processor().process_pending()
        deliveries = [c.args[0] for c in self.send.await_args_list]
        self.assertEqual([d.lead_minutes for d in deliveries], [30, 10, 30, 10])
        self.assertEqual(len({d.event_id for d in deliveries}), 2)
        with self.sessions() as session:
            self.assertEqual(len(session.scalars(select(EventReminder)).all()), 4)
        self.assertEqual(len(self.rows(series)), 2)

    async def test_late_restart_suppresses_duplicates_and_skips_older_threshold(self):
        self.create()
        self.now = self.start - timedelta(minutes=5)
        await self.processor().process_pending()
        self.engine.dispose()
        self.connect()
        await self.processor().process_pending()
        self.send.assert_awaited_once()
        with self.sessions() as session:
            self.assertEqual({r.lead_minutes: r.status for r in session.scalars(select(EventReminder))},
                             {30: "skipped", 10: "sent"})

    async def test_backfilled_and_just_expired_occurrences_never_send_or_complete(self):
        series = self.create(start=self.start - 121 * WEEK)
        self.now = self.start
        for _ in range(4):
            await self.processor().process_pending()
        self.send.assert_not_awaited()
        rows = self.rows(series)
        self.assertEqual(len(rows), 123)
        self.assertTrue(all(e.status == "scheduled" for e in rows))
        with self.sessions() as session:
            self.assertEqual(session.scalars(select(EventReminder)).all(), [])

    async def test_weekly_reminder_channels_are_tenant_isolated(self):
        for alliance in (1, 2, 3):
            self.create(alliance)
        await self.processor().process_pending()
        self.assertEqual({(d.args[0].guild_id, d.args[0].alliance_id, d.args[0].channel_id)
                          for d in self.send.await_args_list}, {(1, 1, 101), (1, 2, 102), (2, 3, 201)})

    def test_metadata_edit_preserves_id_time_claim_and_history_snapshots(self):
        series = self.create(start=self.start - WEEK)
        past, future = self.rows(series)
        processor = self.processor()
        delivery = processor.claim(future.id)
        self.edit(series, name="New", description="Changed")
        rows = self.rows(series)
        self.assertEqual([(e.id, e.starts_at) for e in rows], [(past.id, past.starts_at), (future.id, future.starts_at)])
        self.assertEqual((rows[0].name, rows[0].description), ("Duel", "Prepare"))
        self.assertEqual((rows[1].name, rows[1].description), ("New", "Changed"))
        self.assertEqual(processor.current_delivery(delivery).event_name, "New")
        processor.mark_sent(delivery)
        with self.sessions() as session:
            self.assertEqual(session.get(EventReminder, (future.id, 30)).status, "sent")

    def test_metadata_edit_during_backfill_preserves_old_text(self):
        series = self.create(start=self.start - 121 * WEEK)
        self.edit(series, name="New")
        for _ in range(3):
            ensure_occurrences(self.sessions, self.now)
        rows = self.rows(series)
        self.assertEqual(len(rows), 122)
        self.assertTrue(all(e.name == "Duel" for e in rows if e.starts_at <= self.now))
        self.assertEqual(rows[-1].name, "New")

    def test_same_values_do_not_change_schedule_or_reminder_state(self):
        series = self.create()
        row = self.rows(series)[0]
        delivery = self.processor().claim(row.id)
        self.edit(series, name="Duel", description="Prepare", weekday=1, time_at="17:00")
        self.assertEqual(self.rows(series)[0].schedule_id, row.schedule_id)
        self.assertEqual(self.processor().current_delivery(delivery), delivery)

    def test_schedule_edit_preserves_history_cancels_old_future_and_invalidates_claim(self):
        series = self.create(start=self.start - WEEK)
        past, future = self.rows(series)
        with self.sessions() as session:
            session.add(EventReminder(event_id=past.id, lead_minutes=30, status="sent",
                                      claim_token="historical", recorded_at=past.starts_at - timedelta(minutes=30)))
            session.commit()
        processor = self.processor()
        delivery = processor.claim(future.id)
        self.edit(series, weekday=6, time_at="23:30")
        rows = self.rows(series)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0].starts_at, past.starts_at)
        self.assertEqual(rows[1].status, "cancelled")
        self.assertEqual(rows[2].starts_at, datetime(2026, 9, 28, 1, 30))
        self.assertIsNone(processor.current_delivery(delivery))
        processor.mark_sent(delivery)
        with self.sessions() as session:
            self.assertEqual(session.get(EventReminder, (future.id, 30)).status, "claimed")
            self.assertIsNone(session.get(EventReminder, (future.id, 30)).claim_token)
            self.assertEqual(session.get(EventReminder, (past.id, 30)).claim_token, "historical")
        ensure_occurrences(self.sessions, self.now)
        self.assertEqual(len(self.rows(series)), 3)

    def test_change_away_and_back_has_new_occurrence_and_fresh_claim(self):
        series = self.create()
        old = self.processor().claim(self.rows(series)[0].id)
        self.edit(series, time_at="18:00")
        self.edit(series, time_at="17:00")
        active = [e for e in self.rows(series) if e.status == "scheduled"]
        self.assertEqual(len(active), 1)
        new = self.processor().claim(active[0].id)
        self.assertNotEqual(new.event_id, old.event_id)
        self.assertNotEqual(new.claim_token, old.claim_token)
        self.processor().mark_sent(old)
        self.assertIsNotNone(self.processor().current_delivery(new))

    def test_schedule_edit_and_stop_finish_old_backlog_without_regeneration(self):
        series = self.create(start=self.start - 121 * WEEK)
        self.edit(series, time_at="18:00")
        self.stop(series)
        for _ in range(4):
            ensure_occurrences(self.sessions, self.now + 5 * WEEK)
        rows = self.rows(series)
        self.assertEqual(sum(e.starts_at <= self.now for e in rows), 121)
        self.assertEqual(sum(e.status == "cancelled" for e in rows), 2)
        self.assertTrue(all(e.status == "scheduled" for e in rows if e.starts_at <= self.now))
        self.assertEqual(len(rows), 123)

    async def test_stop_retains_history_and_reminders_but_never_sends_future(self):
        series = self.create(start=self.start - WEEK)
        past, future = self.rows(series)
        claim = self.processor().claim(future.id)
        self.stop(series)
        self.stop(series)
        self.assertIsNone(self.processor().current_delivery(claim))
        await self.processor().process_pending()
        self.send.assert_not_awaited()
        self.assertEqual([e.id for e in self.rows(series)], [past.id, future.id])
        with self.sessions() as session:
            self.assertFalse(session.get(EventSeries, series).active)
            self.assertIsNotNone(session.get(EventReminder, (future.id, 30)))
        with self.assertRaisesRegex(EventManagementError, "stopped"):
            self.edit(series, name="No")

    async def test_in_flight_send_completion_cannot_update_obsolete_claim(self):
        series = self.create()
        occurrence_id = self.rows(series)[0].id
        async def sending(delivery):
            self.edit(series, time_at="18:00")
        processor = ReminderProcessor(self.sessions, sending, lambda: self.now)
        await processor.process_pending()
        with self.sessions() as session:
            reminder = session.get(EventReminder, (occurrence_id, 30))
            self.assertEqual(reminder.status, "claimed")
            self.assertIsNone(reminder.sent_at)

    def test_concurrent_generation_is_unique(self):
        series = self.create()
        self.now = self.start + 70 * WEEK
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda _: ensure_occurrences(self.sessions, self.now), range(3)))
        rows = self.rows(series)
        self.assertEqual(len(rows), 72)
        self.assertEqual(len({(e.schedule_id, e.nominal_at) for e in rows}), 72)

    def test_generation_edit_and_claim_race_has_only_current_live_occurrence(self):
        series = self.create()
        old_id = self.rows(series)[0].id
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [
                pool.submit(ensure_occurrences, self.sessions, self.now),
                pool.submit(self.edit, series, time_at="18:00"),
                pool.submit(self.processor().claim, old_id),
            ]
            results = [f.result() for f in futures]
        ensure_occurrences(self.sessions, self.now)
        rows = self.rows(series)
        self.assertEqual(len(rows), 2)
        self.assertEqual([e.starts_at for e in rows if e.status == "scheduled"], [self.start + timedelta(hours=1)])
        if results[2] is not None:
            self.assertIsNone(self.processor().current_delivery(results[2]))

    def test_generation_stop_and_claim_race_cannot_leave_live_work(self):
        series = self.create()
        occurrence_id = self.rows(series)[0].id
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(ensure_occurrences, self.sessions, self.now),
                       pool.submit(self.stop, series),
                       pool.submit(self.processor().claim, occurrence_id)]
            results = [f.result() for f in futures]
        self.assertEqual([e.status for e in self.rows(series)], ["cancelled"])
        self.assertIsNone(self.processor().claim(occurrence_id))
        if results[2] is not None:
            self.assertIsNone(self.processor().current_delivery(results[2]))

    def test_failed_backfill_transaction_does_not_advance_cursor_or_leave_rows(self):
        series = self.create()
        self.now = self.start + 10 * WEEK
        with self.sessions() as session:
            cursor = session.get(WeeklySchedule, self.rows(series)[0].schedule_id).next_slot_at
        with patch("lastz_bot.recurrence._insert_slot", side_effect=RuntimeError("injected failure")):
            with self.assertRaises(RuntimeError):
                ensure_occurrences(self.sessions, self.now)
        self.assertEqual(len(self.rows(series)), 1)
        with self.sessions() as session:
            self.assertEqual(session.get(WeeklySchedule, self.rows(series)[0].schedule_id).next_slot_at, cursor)
        ensure_occurrences(self.sessions, self.now)
        self.assertEqual(len(self.rows(series)), 12)

    def test_exception_identity_survives_metadata_edit_and_generation(self):
        series = self.create()
        original = self.rows(series)[0]
        with self.sessions() as session:
            row = session.get(Event, original.id)
            row.starts_at += timedelta(hours=1)
            row.is_exception = True
            row.name = "Exception"
            session.commit()
        self.edit(series, name="Template")
        ensure_occurrences(self.sessions, self.now)
        rows = self.rows(series)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].id, rows[0].nominal_at, rows[0].name),
                         (original.id, original.nominal_at, "Exception"))
        self.now = self.start + WEEK - timedelta(minutes=30)
        ensure_occurrences(self.sessions, self.now)
        self.assertEqual(self.rows(series)[-1].starts_at, self.start + WEEK)

    def test_cancelled_slot_is_not_regenerated_by_metadata_edit(self):
        series = self.create()
        original = self.rows(series)[0]
        with self.sessions() as session:
            row = session.get(Event, original.id)
            row.status, row.is_exception = "cancelled", True
            session.commit()
        self.edit(series, description="New")
        ensure_occurrences(self.sessions, self.now)
        self.assertEqual([(e.id, e.status) for e in self.rows(series)], [(original.id, "cancelled")])

    def test_database_enforces_tenant_schedule_and_slot_integrity(self):
        first = self.create()
        second = self.create(3)
        row = self.rows(first)[0]
        foreign = self.rows(second)[0]
        for sql, values in [
            ("UPDATE events SET alliance_id=3 WHERE id=:id", {"id": row.id}),
            ("UPDATE events SET schedule_id=:foreign WHERE id=:id", {"id": row.id, "foreign": foreign.schedule_id}),
            ("UPDATE events SET series_id=NULL WHERE id=:id", {"id": row.id}),
            ("UPDATE events SET status='attended' WHERE id=:id", {"id": row.id}),
        ]:
            with self.subTest(sql=sql), self.assertRaises(IntegrityError), self.engine.begin() as connection:
                connection.execute(text(sql), values)
        with self.assertRaises(IntegrityError), self.sessions() as session:
            session.add(Event(alliance_id=1, series_id=first, schedule_id=row.schedule_id,
                              nominal_at=row.nominal_at, starts_at=row.starts_at, name="Duplicate",
                              created_by_discord_user_id=10))
            session.commit()
        with self.assertRaises(IntegrityError), self.sessions() as session:
            session.add(WeeklySchedule(series_id=first, anchor_at=row.nominal_at,
                                       next_slot_at=row.nominal_at, name="Duplicate rule"))
            session.commit()

    def test_r4_r5_and_admin_can_edit_and_stop(self):
        for actor, administrator in [(10, False), (20, False), (999, True)]:
            with self.subTest(actor=actor):
                series = self.create()
                edit_series(self.sessions, 1, series, actor, administrator, name="Edited", now=self.now)
                stop_series(self.sessions, 1, series, actor, administrator, now=self.now)
                with self.sessions() as session:
                    self.assertEqual(session.get(EventSeries, series).name, "Edited")
                    self.assertFalse(session.get(EventSeries, series).active)

    def test_denied_and_unknown_series_ids_are_indistinguishable_and_unchanged(self):
        series = self.create()
        foreign = self.create(3)
        cases = [(1, series, 30, False), (1, series, 999, False), (1, series, 40, False),
                 (1, series, 50, False), (2, series, 50, False), (2, series, 999, True),
                 (1, foreign, 10, False), (1, foreign, 999, True), (1, 99999, 999, True)]
        errors = set()
        for guild, series_id, actor, admin in cases:
            for operation in (edit_series, stop_series):
                kwargs = {"name": "Denied"} if operation is edit_series else {}
                with self.subTest(case=(guild, series_id, actor, admin), op=operation.__name__):
                    with self.assertRaises(EventManagementError) as caught:
                        operation(self.sessions, guild, series_id, actor, admin, now=self.now, **kwargs)
                    errors.add(str(caught.exception))
        self.assertEqual(len(errors), 1)
        with self.sessions() as session:
            self.assertTrue(all(s.active and s.name == "Duel" for s in session.scalars(select(EventSeries))))

    def test_weekly_occurrence_ids_cannot_be_destructively_managed_as_once(self):
        series = self.create()
        occurrence_id = self.rows(series)[0].id
        for operation in (edit_event, delete_event):
            kwargs = {"name": "No"} if operation is edit_event else {}
            with self.assertRaisesRegex(EventManagementError, "weekly occurrence"):
                operation(self.sessions, 1, occurrence_id, 10, False, **kwargs)
            errors = []
            for guild, actor, event_id in [(1, 40, occurrence_id), (2, 50, occurrence_id), (1, 10, 99999)]:
                with self.assertRaises(EventManagementError) as caught:
                    operation(self.sessions, guild, event_id, actor, False, **kwargs)
                errors.append(str(caught.exception))
            self.assertEqual(len(set(errors)), 1)
        self.assertEqual(len(self.rows(series)), 1)

    async def test_series_command_callbacks_and_dm_denial(self):
        series = self.create()
        interaction = self.interaction()
        await self.commands["edit-series"](interaction, series, name="Changed", weekday="Sunday", time_at="23:30")
        self.assertIn("updated", interaction.response.send_message.call_args.args[0])
        interaction = self.interaction()
        await self.commands["stop-series"](interaction, series)
        self.assertIn("history is preserved", interaction.response.send_message.call_args.args[0])
        for command, kwargs in [("edit-series", {"name": "No"}), ("stop-series", {})]:
            interaction = self.interaction(guild=None)
            await self.commands[command](interaction, series, **kwargs)
            self.assertIn("inside a Discord server", interaction.response.send_message.call_args.args[0])

    async def test_weekly_create_permission_denials_and_admin_override(self):
        for actor in (30, 40, 50, 999):
            interaction = self.interaction(actor=actor)
            await self.commands["create"](interaction, "Alpha", "No", "2026-09-22 17:00", recurrence="weekly")
            self.assertIn("need to be an R4", interaction.response.send_message.call_args.args[0])
        self.assertEqual(self.rows(), [])
        interaction = self.interaction(actor=999, admin=True)
        await self.commands["create"](interaction, "Alpha", "Allowed", "2026-09-22 17:00", recurrence="weekly")
        self.assertEqual(len(self.rows()), 1)

    def test_invalid_series_edits_are_atomic(self):
        series = self.create()
        for kwargs in ({}, {"name": " "}, {"time_at": "25:00"}, {"weekday": 7}):
            with self.subTest(kwargs=kwargs), self.assertRaises(EventManagementError):
                self.edit(series, **kwargs)
        self.assertEqual(len(self.rows(series)), 1)
        self.assertEqual(self.rows(series)[0].name, "Duel")
