"""RSVP intentions, participation snapshots, permissions, and serialized races."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import delete, event as sqlalchemy_event, select, text
from sqlalchemy.exc import IntegrityError

from lastz_bot.database.models import Alliance, Event, EventRSVP, EventSeries, Member, WeeklySchedule
from lastz_bot.event_management import EventManagementError, delete_event, edit_event
from lastz_bot.recurrence import WEEK, create_weekly, edit_series, ensure_occurrences, stop_series
from lastz_bot.reminders import ReminderProcessor
from lastz_bot.rsvp import RSVPSummary, get_rsvps, set_rsvp, summary_pages
import test_weekly_events as weekly


class ParticipationTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the file-backed multi-tenant fixture, not its test methods.
    connect = weekly.WeeklyTests.connect
    rows = weekly.WeeklyTests.rows
    interaction = weekly.WeeklyTests.interaction

    def setUp(self):
        weekly.WeeklyTests.setUp(self)
        for target in ("lastz_bot.rsvp.utc_now_naive", "lastz_bot.event_management.utc_now_naive"):
            clock = patch(target, side_effect=lambda: self.now)
            clock.start()
            self.addCleanup(clock.stop)

    def once(self, participation="optional", alliance_id=1):
        with self.sessions() as session:
            row = Event(alliance_id=alliance_id, name="Duel", starts_at=self.start,
                        participation=participation, created_by_discord_user_id=10)
            session.add(row)
            session.commit()
            return row.id

    def series(self, participation="optional", start=None):
        with self.sessions() as session:
            result = create_weekly(session, session.get(Alliance, 1), "Weekly", "Prepare",
                                   start or self.start, 10, self.now, participation=participation)
            session.commit()
            return result.id

    def rsvp(self, occurrence, response="going", actor=30, guild=1):
        set_rsvp(self.sessions, guild, occurrence, actor, response)

    def edit(self, occurrence, **kwargs):
        edit_event(self.sessions, 1, occurrence, 10, False, **kwargs)

    def edit_series(self, series, **kwargs):
        edit_series(self.sessions, 1, series, 10, False, now=self.now, **kwargs)

    def snapshot(self):
        with self.sessions() as session:
            return session.execute(select(
                EventRSVP.event_id, EventRSVP.discord_user_id, EventRSVP.response,
                EventRSVP.created_at, EventRSVP.updated_at,
            ).order_by(EventRSVP.event_id, EventRSVP.discord_user_id)).all()

    async def test_create_once_modes_and_default(self):
        for mode in (None, "none", "optional", "required"):
            options = {} if mode is None else {"participation": mode}
            interaction = self.interaction()
            await self.commands["create"](interaction, "Alpha", str(mode), "2026-09-22 17:00", **options)
            self.assertIn("created", interaction.followup.send.call_args.args[0])
        self.assertEqual([row.participation for row in self.rows()], ["none", "none", "optional", "required"])

    async def test_list_shows_enabled_modes_without_none_clutter(self):
        for mode in ("none", "optional", "required"):
            self.once(mode)
        interaction = self.interaction()
        await self.commands["list"](interaction, "Alpha")
        message = interaction.followup.send.call_args.args[0]
        self.assertEqual(message.count("RSVP:"), 2)
        self.assertIn("RSVP: optional", message)
        self.assertIn("RSVP: required", message)
        self.assertNotIn("RSVP: none", message)

    async def test_weekly_create_command_inherits_all_modes(self):
        for mode in ("none", "optional", "required"):
            interaction = self.interaction()
            await self.commands["create"](interaction, "Alpha", mode, "2026-09-22 17:00",
                                          recurrence="weekly", participation=mode)
        with self.sessions() as session:
            self.assertEqual([s.participation for s in session.scalars(select(EventSeries).order_by(EventSeries.id))],
                             ["none", "optional", "required"])
        self.assertEqual([r.participation for r in self.rows()], ["none", "optional", "required"])

    def test_weekly_mode_edit_updates_future_retains_ids_rsvps_and_reminder_claims(self):
        series = self.series(start=self.start - WEEK)
        past, future = self.rows(series)
        self.rsvp(future.id)
        before = self.snapshot()
        processor = ReminderProcessor(self.sessions, AsyncMock(), lambda: self.now)
        claim = processor.claim(future.id)
        self.edit_series(series, participation="required")
        rows = self.rows(series)
        self.assertEqual([(r.id, r.participation) for r in rows],
                         [(past.id, "optional"), (future.id, "required")])
        self.assertEqual(self.snapshot(), before)
        self.assertIsNotNone(processor.current_delivery(claim))
        self.now = self.start + WEEK
        ensure_occurrences(self.sessions, self.now)
        self.assertEqual(self.rows(series)[-1].participation, "required")

    def test_series_mode_change_uses_clock_after_acquiring_write_lock(self):
        series = self.series()
        occurrence = self.rows(series)[0].id
        def advance_clock(connection, cursor, statement, parameters, context, executemany):
            if statement == "BEGIN IMMEDIATE":
                self.now = self.start
        sqlalchemy_event.listen(self.engine, "after_cursor_execute", advance_clock)
        try:
            edit_series(self.sessions, 1, series, 10, False, participation="required")
        finally:
            sqlalchemy_event.remove(self.engine, "after_cursor_execute", advance_clock)
        with self.sessions() as session:
            self.assertEqual(session.get(Event, occurrence).participation, "optional")
        self.assertEqual(self.rows(series)[-1].participation, "required")

    def test_series_disable_and_reenable_retains_existing_intentions(self):
        series = self.series()
        occurrence = self.rows(series)[0].id
        self.rsvp(occurrence)
        before = self.snapshot()
        self.edit_series(series, participation="none")
        with self.assertRaisesRegex(EventManagementError, "disabled"):
            self.rsvp(occurrence, "maybe")
        self.assertEqual(self.snapshot(), before)
        self.edit_series(series, participation="optional")
        self.assertEqual(self.snapshot(), before)
        self.rsvp(occurrence, "maybe")
        self.assertEqual(self.snapshot()[0].response, "maybe")

    def test_backfill_uses_closed_rule_mode_after_edit_and_stop(self):
        series = self.series(start=self.start - 121 * WEEK)
        self.edit_series(series, participation="required")
        stop_series(self.sessions, 1, series, 10, False, now=self.now)
        for _ in range(4):
            ensure_occurrences(self.sessions, self.now + 10 * WEEK)
        rows = self.rows(series)
        self.assertEqual(len(rows), 122)
        self.assertTrue(all(r.participation == "optional" for r in rows if r.starts_at <= self.now))
        self.assertEqual((rows[-1].participation, rows[-1].status), ("required", "cancelled"))

    def test_participation_override_independent_of_other_exceptions(self):
        series = self.series()
        occurrence = self.rows(series)[0]
        with self.sessions() as session:
            row = session.get(Event, occurrence.id)
            row.participation_overridden, row.participation = True, "none"
            session.commit()
        self.edit_series(series, participation="required")
        self.assertEqual(self.rows(series)[0].participation, "none")
        self.now = self.start + WEEK
        ensure_occurrences(self.sessions, self.now)
        self.assertEqual(self.rows(series)[-1].participation, "required")

    def test_schedule_change_preserves_old_rsvps_and_new_occurrence_has_no_rsvps(self):
        series = self.series()
        original = self.rows(series)[0]
        self.rsvp(original.id)
        before = self.snapshot()
        self.edit_series(series, participation="required", time_at="18:00")
        old, new = self.rows(series)
        self.assertEqual((old.status, old.participation), ("cancelled", "optional"))
        self.assertEqual(new.participation, "required")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(get_rsvps(self.sessions, 1, new.id, 10, False).groups["going"], ())
        self.assertEqual(get_rsvps(self.sessions, 1, old.id, 10, False).groups["going"], (30,))

    def test_ordinary_member_can_create_change_and_repeat_one_response(self):
        occurrence = self.once()
        self.rsvp(occurrence)
        first = self.snapshot()[0]
        self.now += timedelta(seconds=1)
        self.rsvp(occurrence, "maybe")
        self.rsvp(occurrence, "not_going")
        self.rsvp(occurrence, "not_going")
        current, = self.snapshot()
        self.assertEqual(current[:3], (occurrence, 30, "not_going"))
        self.assertEqual(current.created_at, first.created_at)
        self.assertEqual(current.updated_at, self.now)
        self.assertIsNone(current.updated_at.tzinfo)

    def test_none_blocks_new_rsvp(self):
        occurrence = self.once("none")
        with self.assertRaisesRegex(EventManagementError, "disabled"):
            self.rsvp(occurrence)
        self.assertEqual(self.snapshot(), [])

    def test_disable_retains_summary_and_reenable_keeps_existing_response(self):
        occurrence = self.once()
        self.rsvp(occurrence)
        before = self.snapshot()
        self.edit(occurrence, participation="none")
        for actor in (30, 20):
            with self.assertRaisesRegex(EventManagementError, "disabled"):
                self.rsvp(occurrence, "maybe", actor=actor)
        self.assertEqual(self.snapshot(), before)
        summary = get_rsvps(self.sessions, 1, occurrence, 10, False)
        self.assertEqual(summary.groups["going"], (30,))
        self.assertIn("retained", summary_pages(summary)[0])
        self.edit(occurrence, participation="required")
        self.assertEqual(self.snapshot(), before)
        self.rsvp(occurrence, "maybe")
        self.assertEqual(self.snapshot()[0].response, "maybe")

    def test_one_time_reschedule_retains_exact_rsvp_without_reconfirmation(self):
        occurrence = self.once()
        self.rsvp(occurrence)
        before = self.snapshot()
        self.now += timedelta(minutes=1)
        self.edit(occurrence, starts_at="2026-09-23 17:00")
        self.assertEqual(self.snapshot(), before)
        self.assertIn("not attendance or reconfirmation", summary_pages(
            get_rsvps(self.sessions, 1, occurrence, 10, False))[0])

    def test_expired_and_exact_start_block_both_new_and_changed_responses(self):
        occurrence = self.once("required")
        self.rsvp(occurrence)
        before = self.snapshot()
        for now in (self.start, self.start + timedelta(microseconds=1)):
            self.now = now
            for actor in (30, 20):
                with self.assertRaisesRegex(EventManagementError, "closed"):
                    self.rsvp(occurrence, "maybe", actor)
        self.assertEqual(self.snapshot(), before)

    def test_cancelled_and_stopped_weekly_occurrences_reject_rsvp_but_keep_summary(self):
        series = self.series()
        occurrence = self.rows(series)[0].id
        self.rsvp(occurrence)
        before = self.snapshot()
        stop_series(self.sessions, 1, series, 10, False, now=self.now)
        with self.assertRaisesRegex(EventManagementError, "closed"):
            self.rsvp(occurrence, "maybe")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(get_rsvps(self.sessions, 1, occurrence, 10, False).groups["going"], (30,))

    def test_cancelled_override_on_active_series_rejects_rsvp(self):
        series = self.series()
        occurrence = self.rows(series)[0].id
        with self.sessions() as session:
            session.get(Event, occurrence).status = "cancelled"
            session.commit()
        with self.assertRaisesRegex(EventManagementError, "closed"):
            self.rsvp(occurrence)

    def test_cross_tenant_unknown_unlinked_and_nonmember_denial_are_indistinguishable(self):
        local = self.once()
        other = self.once(alliance_id=2)
        foreign = self.once(alliance_id=3)
        errors = set()
        for event_id, actor, guild in ((local, 40, 1), (local, 999, 1), (local, 10, 2),
                                       (other, 30, 1), (foreign, 30, 1), (999999, 30, 1)):
            with self.assertRaises(EventManagementError) as caught:
                self.rsvp(event_id, actor=actor, guild=guild)
            errors.add(str(caught.exception))
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.snapshot(), [])

    async def test_admin_without_alliance_membership_cannot_rsvp(self):
        occurrence = self.once()
        interaction = self.interaction(actor=999, admin=True)
        await self.commands["rsvp"](interaction, occurrence, "going")
        self.assertIn("not a linked member", interaction.followup.send.call_args.args[0])
        self.assertEqual(self.snapshot(), [])

    def test_membership_removed_blocks_updates_without_deleting_intention(self):
        occurrence = self.once()
        self.rsvp(occurrence)
        before = self.snapshot()
        with self.sessions() as session:
            session.execute(delete(Member).where(Member.alliance_id == 1, Member.discord_user_id == 30))
            session.commit()
        with self.assertRaises(EventManagementError):
            self.rsvp(occurrence, "maybe")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(get_rsvps(self.sessions, 1, occurrence, 10, False).groups["going"], (30,))

    def test_r4_r5_admin_summary_access_and_all_group_counts(self):
        occurrence = self.once()
        for actor, response in ((30, "going"), (10, "maybe"), (20, "not_going")):
            self.rsvp(occurrence, response, actor)
        for actor, admin in ((10, False), (20, False), (999, True)):
            summary = get_rsvps(self.sessions, 1, occurrence, actor, admin)
            self.assertEqual(summary.groups, {"going": (30,), "maybe": (10,), "not_going": (20,)})
            message = summary_pages(summary)[0]
            for label in ("Going (1)", "Maybe (1)", "Not Going (1)"):
                self.assertIn(label, message)

    def test_unauthorized_summary_and_foreign_admin_denied(self):
        local = self.once()
        foreign = self.once(alliance_id=3)
        errors = set()
        for occurrence, guild, actor, admin in ((local, 1, 30, False), (local, 1, 40, False),
                                                (local, 1, 999, False), (local, 2, 999, True),
                                                (foreign, 1, 999, True), (99999, 1, 999, True)):
            with self.assertRaises(EventManagementError) as caught:
                get_rsvps(self.sessions, guild, occurrence, actor, admin)
            errors.add(str(caught.exception))
        self.assertEqual(len(errors), 1)

    def test_summary_pages_are_bounded_and_include_every_user_once(self):
        users = tuple(range(100000000000000000, 100000000000000350))
        pages = summary_pages(RSVPSummary(1, "required", {"going": users, "maybe": (), "not_going": ()}))
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(page) <= 1900 for page in pages))
        joined = "\n".join(pages)
        self.assertIn("Going (350)", joined)
        for user_id in users:
            self.assertEqual(joined.count(f"<@{user_id}>"), 1)

    def test_database_enforces_response_uniqueness_and_foreign_key(self):
        occurrence = self.once()
        self.rsvp(occurrence)
        for occurrence_id, actor, response in ((occurrence, 30, "maybe"), (99999, 30, "going"),
                                                 (occurrence, 20, "attended")):
            with self.subTest(values=(occurrence_id, actor, response)), self.assertRaises(IntegrityError):
                with self.sessions() as session:
                    session.add(EventRSVP(event_id=occurrence_id, discord_user_id=actor, response=response,
                                          created_at=self.now, updated_at=self.now))
                    session.commit()

    def test_database_rejects_invalid_participation(self):
        self.once()
        self.series()
        for table in ("events", "event_series", "weekly_schedules"):
            with self.subTest(table=table), self.assertRaises(IntegrityError), self.engine.begin() as connection:
                connection.execute(text(f"UPDATE {table} SET participation='mandatory'"))

    def test_event_delete_cascades_only_its_rsvps(self):
        first, second = self.once(), self.once()
        self.rsvp(first)
        self.rsvp(second)
        delete_event(self.sessions, 1, first, 10, False)
        self.assertEqual([r.event_id for r in self.snapshot()], [second])

    def test_sql_occurrence_delete_cascades_rsvps(self):
        series = self.series()
        occurrence = self.rows(series)[0].id
        self.rsvp(occurrence)
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM events WHERE id=:id"), {"id": occurrence})
        self.assertEqual(self.snapshot(), [])

    def test_concurrent_responses_upsert_one_record(self):
        occurrence = self.once()
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda response: self.rsvp(occurrence, response), ("going", "maybe", "not_going")))
        self.assertEqual(len(self.snapshot()), 1)
        self.assertIn(self.snapshot()[0].response, ("going", "maybe", "not_going"))

    def test_disable_and_response_race_is_serialized(self):
        occurrence = self.once()
        def respond():
            try:
                self.rsvp(occurrence)
                return True
            except EventManagementError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            response = pool.submit(respond)
            disable = pool.submit(self.edit, occurrence, participation="none")
            accepted = response.result()
            disable.result()
        self.assertEqual(len(self.snapshot()), int(accepted))
        with self.assertRaisesRegex(EventManagementError, "disabled"):
            self.rsvp(occurrence, "maybe")

    def test_stop_generation_and_response_race_preserves_valid_intentions_only(self):
        series = self.series()
        occurrence = self.rows(series)[0].id
        def respond():
            try:
                self.rsvp(occurrence)
                return True
            except EventManagementError:
                return False
        with ThreadPoolExecutor(max_workers=3) as pool:
            response = pool.submit(respond)
            generation = pool.submit(ensure_occurrences, self.sessions, self.now)
            stop = pool.submit(stop_series, self.sessions, 1, series, 10, False, now=self.now)
            accepted = response.result()
            generation.result()
            stop.result()
        self.assertEqual(len(self.snapshot()), int(accepted))
        with self.assertRaisesRegex(EventManagementError, "closed"):
            self.rsvp(occurrence)

    def test_delete_and_response_race_cannot_leave_orphan(self):
        occurrence = self.once()
        def respond():
            try:
                self.rsvp(occurrence)
            except EventManagementError:
                pass
        with ThreadPoolExecutor(max_workers=2) as pool:
            response = pool.submit(respond)
            deletion = pool.submit(delete_event, self.sessions, 1, occurrence, 10, False)
            response.result()
            deletion.result()
        self.assertEqual(self.snapshot(), [])

    async def test_edit_commands_accept_participation_only(self):
        occurrence = self.once("none")
        interaction = self.interaction()
        await self.commands["edit"](interaction, occurrence, participation="optional")
        self.assertIn("updated", interaction.followup.send.call_args.args[0])
        self.assertEqual(self.rows()[0].participation, "optional")
        series = self.series()
        interaction = self.interaction()
        await self.commands["edit-series"](interaction, series, participation="required")
        self.assertEqual(self.rows(series)[0].participation, "required")

    def test_participation_edits_reuse_management_permissions(self):
        occurrence = self.once()
        series = self.series()
        for actor, admin in ((10, False), (20, False), (999, True)):
            edit_event(self.sessions, 1, occurrence, actor, admin, participation="required")
            edit_series(self.sessions, 1, series, actor, admin, participation="required", now=self.now)
        for actor in (30, 40, 999):
            with self.assertRaises(EventManagementError):
                edit_event(self.sessions, 1, occurrence, actor, False, participation="none")
            with self.assertRaises(EventManagementError):
                edit_series(self.sessions, 1, series, actor, False, participation="none", now=self.now)

    async def test_rsvp_commands_are_ephemeral_and_dm_denied(self):
        occurrence = self.once()
        interaction = self.interaction(actor=30)
        await self.commands["rsvp"](interaction, occurrence, "going")
        self.assertTrue(interaction.followup.send.call_args.kwargs["ephemeral"])
        interaction = self.interaction()
        await self.commands["rsvps"](interaction, occurrence)
        call = interaction.followup.send.call_args
        self.assertTrue(call.kwargs["ephemeral"])
        self.assertFalse(call.kwargs["allowed_mentions"].everyone)
        self.assertIn("Going (1)", call.args[0])
        for command, extra in (("rsvp", ("going",)), ("rsvps", ())):
            interaction = self.interaction(guild=None)
            await self.commands[command](interaction, occurrence, *extra)
            self.assertIn("inside a Discord server", interaction.followup.send.call_args.args[0])

    def test_invalid_values_rejected_without_writes(self):
        occurrence = self.once()
        series = self.series()
        for operation in (
            lambda: self.rsvp(occurrence, "attended"),
            lambda: self.edit(occurrence, participation="invalid"),
            lambda: self.edit_series(series, participation="invalid"),
        ):
            with self.assertRaises(EventManagementError):
                operation()
        self.assertEqual(self.snapshot(), [])
        self.assertTrue(all(row.participation == "optional" for row in self.rows()))
