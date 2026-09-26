from interaction_fakes import transport
from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from lastz_bot.commands.event import setup_event_commands
from lastz_bot.database.base import Base
from lastz_bot.database.models import Alliance, Event, EventReminder, Guild, Member


class EventCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        for target in (
            "lastz_bot.commands.event.SessionLocal",
            "lastz_bot.permissions.SessionLocal",
        ):
            patcher = patch(target, self.sessions)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.now = datetime(2026, 9, 25, 18)
        for target in ("lastz_bot.commands.event.utc_now_naive", "lastz_bot.event_management.utc_now_naive"):
            clock = patch(target, side_effect=lambda: self.now)
            clock.start()
            self.addCleanup(clock.stop)

        # Exercise the registered callbacks without connecting to Discord.
        tree = Mock()
        setup_event_commands(tree)
        group = tree.add_command.call_args.args[0]
        self.create = group.get_command("create").callback
        self.list_events = group.get_command("list").callback
        self.edit = group.get_command("edit").callback
        self.delete = group.get_command("delete").callback

        with self.sessions() as session:
            session.add_all([Guild(id=1001, name="One"), Guild(id=2002, name="Two")])
            session.flush()
            session.add_all([
                Alliance(id=1, guild_id=1001, name="Alpha"),
                Alliance(id=2, guild_id=1001, name="Bravo"),
                Alliance(id=3, guild_id=2002, name="Alpha"),
                Alliance(id=4, guild_id=2002, name="Foreign"),
            ])
            session.flush()
            session.add_all([
                Member(alliance_id=1, game_name="Officer", rank="R4", discord_user_id=10),
                Member(alliance_id=1, game_name="Leader", rank="R5", discord_user_id=20),
                Member(alliance_id=1, game_name="Member", rank="R1", discord_user_id=30),
                Member(alliance_id=2, game_name="Other officer", rank="R5", discord_user_id=40),
                Member(alliance_id=3, game_name="Foreign officer", rank="R5", discord_user_id=50),
                Member(alliance_id=1, game_name="Local member", rank="R1", discord_user_id=50),
            ])
            session.commit()

    def interaction(self, user_id=10, guild_id=1001, admin=False):
        return SimpleNamespace(
            guild=SimpleNamespace(id=guild_id) if guild_id is not None else None,
            user=SimpleNamespace(
                id=user_id, guild_permissions=SimpleNamespace(administrator=admin),
            ),
            **transport(),
        )

    def assert_response(self, interaction, message):
        interaction.followup.send.assert_awaited_once_with(message, ephemeral=True)

    def stored_events(self):
        with self.sessions() as session:
            return session.scalars(select(Event).order_by(Event.id)).all()

    def add_event(self, alliance_id, name, starts_at, description=None):
        with self.sessions() as session:
            record = Event(
                alliance_id=alliance_id, name=name, starts_at=starts_at,
                description=description, created_by_discord_user_id=20,
            )
            session.add(record)
            session.commit()
            return record.id

    async def test_create_sqlite_storage_and_at_display_round_trip(self):
        interaction = self.interaction()
        await self.create(
            interaction, " Alpha ", " Duel ", " 2026-09-25 17:00 ", " Prepare ",
        )
        self.assert_response(
            interaction,
            "✅ Event `Duel` created for alliance `Alpha` at `2026-09-25 17:00` Apocalypse Time.",
        )
        stored, = self.stored_events()
        self.assertEqual(stored.starts_at, datetime(2026, 9, 25, 19))
        self.assertIsNone(stored.starts_at.tzinfo)
        self.assertEqual((stored.alliance_id, stored.created_by_discord_user_id), (1, 10))
        self.assertEqual((stored.name, stored.description), ("Duel", "Prepare"))
        with self.sessions() as session:
            self.assertEqual(
                session.scalar(text("SELECT starts_at FROM events")),
                "2026-09-25 19:00:00.000000",
            )
        listing = self.interaction(user_id=99)
        await self.list_events(listing, " Alpha ")
        self.assert_response(
            listing,
            "**Upcoming events for `Alpha`:**\n• ID `1` — `2026-09-25 17:00` AT — **Duel** — Prepare",
        )

    async def test_midnight_round_trip_through_sqlite(self):
        interaction = self.interaction()
        await self.create(interaction, "Alpha", "Reset", "2026-12-31 23:30")
        stored, = self.stored_events()
        self.assertEqual(stored.starts_at, datetime(2027, 1, 1, 1, 30))
        self.assertIsNone(stored.starts_at.tzinfo)
        listing = self.interaction()
        await self.list_events(listing, "Alpha")
        self.assert_response(
            listing,
            "**Upcoming events for `Alpha`:**\n• ID `1` — `2026-12-31 23:30` AT — **Reset**",
        )

    async def test_r4_r5_and_unlinked_administrator_can_create(self):
        for user_id, admin in ((10, False), (20, False), (99, True)):
            with self.subTest(user_id=user_id):
                interaction = self.interaction(user_id=user_id, admin=admin)
                await self.create(interaction, "Alpha", "Duel", "2026-09-25 17:00")
                self.assert_response(
                    interaction,
                    "✅ Event `Duel` created for alliance `Alpha` at `2026-09-25 17:00` Apocalypse Time.",
                )
        self.assertEqual([item.created_by_discord_user_id for item in self.stored_events()], [10, 20, 99])

    async def test_members_unlinked_users_and_other_tenant_officers_cannot_create(self):
        # User 50 is R5 in another guild's Alpha but only MEMBER in this Alpha.
        for user_id, guild_id, alliance in (
            (30, 1001, "Alpha"), (99, 1001, "Alpha"),
            (40, 1001, "Alpha"), (50, 1001, "Alpha"),
            (10, 1001, "Bravo"), (10, 2002, "Alpha"),
        ):
            with self.subTest(user_id=user_id, guild_id=guild_id, alliance=alliance):
                interaction = self.interaction(user_id=user_id, guild_id=guild_id)
                await self.create(interaction, alliance, "Duel", "2026-09-25 17:00")
                self.assert_response(
                    interaction,
                    "❌ You need to be an R4, R5, or Server Administrator "
                    "of this alliance to create an event.",
                )
                self.assertEqual(self.stored_events(), [])

    async def test_create_targets_same_named_alliance_in_current_guild(self):
        interaction = self.interaction(user_id=50, guild_id=2002)
        await self.create(interaction, "Alpha", "Foreign duel", "2026-09-25 17:00")
        stored, = self.stored_events()
        self.assertEqual(stored.alliance_id, 3)
        listing = self.interaction()
        await self.list_events(listing, "Alpha")
        self.assert_response(listing, "ℹ️ No upcoming events for alliance `Alpha`.")

    async def test_list_scopes_tenants_orders_and_filters_using_utc_inclusively(self):
        self.add_event(1, "Later", self.now + timedelta(hours=1))
        self.add_event(1, "Now", self.now)
        self.add_event(1, "Past", self.now - timedelta(microseconds=1))
        self.add_event(2, "Other alliance", self.now)
        self.add_event(3, "Other guild", self.now)
        listing = self.interaction(user_id=99)
        await self.list_events(listing, "Alpha")
        self.assert_response(
            listing,
            "**Upcoming events for `Alpha`:**\n"
            "• ID `2` — `2026-09-25 16:00` AT — **Now**\n"
            "• ID `1` — `2026-09-25 17:00` AT — **Later**",
        )

    async def test_invalid_time_keeps_existing_error_and_does_not_write(self):
        interaction = self.interaction()
        await self.create(interaction, "Alpha", "Duel", "2026-02-30 17:00")
        self.assert_response(interaction, "❌ Start time must use format `YYYY-MM-DD HH:MM`.")
        self.assertEqual(self.stored_events(), [])

    async def test_server_and_alliance_guards_for_both_commands(self):
        for command in (self.create, self.list_events):
            for guild_id, alliance, expected in (
                (None, "Alpha", "❌ This command can only be used inside a Discord server."),
                (1001, " ", "❌ Alliance name cannot be empty."),
                (9999, "Alpha", "❌ This Discord server has not been initialized yet. Run `/setup` first."),
                (1001, "Foreign", "❌ Alliance `Foreign` does not exist."),
            ):
                with self.subTest(command=command.__name__, guild_id=guild_id, alliance=alliance):
                    interaction = self.interaction(guild_id=guild_id, admin=True)
                    args = ("Duel", "2026-09-25 17:00") if command is self.create else ()
                    await command(interaction, alliance, *args)
                    self.assert_response(interaction, expected)
                    self.assertEqual(self.stored_events(), [])

    async def test_past_once_create_rejected_without_any_rows(self):
        for recurrence in ({}, {"recurrence": "once"}):
            interaction = self.interaction()
            await self.create(interaction, "Alpha", "Past", "2026-09-24 17:00", **recurrence)
            self.assert_response(interaction,
                "❌ One-time events must start in the future. Choose a later Apocalypse Time.")
        self.assertEqual(self.stored_events(), [])
        with self.sessions() as session:
            for table in ("events", "event_reminders", "event_series", "weekly_schedules"):
                self.assertEqual(session.scalar(text(f"SELECT COUNT(*) FROM {table}")), 0)

    async def test_create_current_minute_rejected_and_next_minute_allowed(self):
        for seconds, micros in ((0, 0), (0, 1), (30, 0), (59, 999999)):
            self.now = datetime(2026, 9, 25, 18, 0, seconds, micros)
            interaction = self.interaction()
            await self.create(interaction, "Alpha", "Now", "2026-09-25 16:00")
            self.assert_response(interaction,
                "❌ One-time events must start in the future. Choose a later Apocalypse Time.")
            self.assertEqual(self.stored_events(), [])
        self.now = datetime(2026, 9, 25, 18, 0, 30)
        interaction = self.interaction()
        await self.create(interaction, "Alpha", "Future", "2026-09-25 16:01")
        self.assert_response(interaction,
            "✅ Event `Future` created for alliance `Alpha` at `2026-09-25 16:01` Apocalypse Time.")
        self.assertEqual(self.stored_events()[0].starts_at, datetime(2026, 9, 25, 18, 1))

    async def test_rejected_past_or_current_reschedule_preserves_all_state(self):
        event_id = self.add_managed_event()
        before = self.reminder_snapshot()
        for start in ("2026-09-24 17:00", "2026-09-25 16:00"):
            for micros in (0, 1):
                self.now = datetime(2026, 9, 25, 18, 0, 0, micros)
                interaction = self.interaction()
                await self.edit(interaction, event_id, starts_at=start, name="Changed", description="Changed")
                self.assert_response(interaction,
                    "❌ One-time events must start in the future. Choose a later Apocalypse Time.")
                stored, = self.stored_events()
                self.assertEqual((stored.name, stored.description, stored.starts_at),
                                 ("Duel", "Prepare", datetime(2026, 9, 25, 19)))
                self.assertEqual(self.reminder_snapshot(), before)

    async def test_reschedule_next_minute_is_allowed(self):
        event_id = self.add_managed_event()
        self.now = datetime(2026, 9, 25, 18, 0, 30)
        await self.edit(self.interaction(), event_id, starts_at="2026-09-25 16:01")
        self.assertEqual(self.stored_events()[0].starts_at, datetime(2026, 9, 25, 18, 1))
        self.assertEqual(self.reminder_snapshot(), [])

    async def test_historical_metadata_and_unchanged_start_still_allowed(self):
        event_id = self.add_managed_event()
        before = self.reminder_snapshot()
        self.now = datetime(2026, 9, 26)
        for kwargs in ({"name": "Renamed"}, {"description": "New"}, {"starts_at": "2026-09-25 17:00"}):
            interaction = self.interaction()
            await self.edit(interaction, event_id, **kwargs)
            self.assertIn("updated", interaction.followup.send.call_args.args[0])
            self.assertEqual(self.reminder_snapshot(), before)
        self.assertEqual(self.stored_events()[0].starts_at, datetime(2026, 9, 25, 19))

    async def test_past_reschedule_does_not_bypass_access_checks(self):
        local = self.add_managed_event()
        foreign = self.add_managed_event(3)
        before = self.reminder_snapshot()
        for event_id, actor, admin in ((local, 30, False), (local, 99, False), (local, 40, False),
                                        (local, 50, False), (foreign, 10, False), (foreign, 99, True)):
            interaction = self.interaction(user_id=actor, admin=admin)
            await self.edit(interaction, event_id, starts_at="2026-09-24 17:00")
            self.assert_response(interaction,
                "❌ Event not found in this server, or you do not have permission to manage it.")
        self.assertEqual(self.reminder_snapshot(), before)

    def add_managed_event(self, alliance_id=1):
        event_id = self.add_event(alliance_id, "Duel", datetime(2026, 9, 25, 19), "Prepare")
        with self.sessions() as session:
            session.add_all([
                EventReminder(
                    event_id=event_id, lead_minutes=30, status="sent",
                    recorded_at=self.now, sent_at=self.now, claim_token="old-30", channel_id=101,
                ),
                EventReminder(
                    event_id=event_id, lead_minutes=10, status="claimed",
                    recorded_at=self.now, claim_token="old-10", channel_id=101,
                ),
            ])
            session.commit()
        return event_id

    def reminder_snapshot(self):
        with self.sessions() as session:
            return session.execute(select(
                EventReminder.event_id, EventReminder.lead_minutes, EventReminder.status,
                EventReminder.claim_token, EventReminder.recorded_at, EventReminder.sent_at,
                EventReminder.channel_id,
            ).order_by(EventReminder.event_id, EventReminder.lead_minutes)).all()

    async def test_list_ids_distinguish_same_named_events(self):
        first = self.add_event(1, "Duel", self.now)
        second = self.add_event(1, "Duel", self.now + timedelta(minutes=1))
        interaction = self.interaction()
        await self.list_events(interaction, "Alpha")
        self.assert_response(interaction,
            f"**Upcoming events for `Alpha`:**\n"
            f"• ID `{first}` — `2026-09-25 16:00` AT — **Duel**\n"
            f"• ID `{second}` — `2026-09-25 16:01` AT — **Duel**",
        )

    async def test_name_edit_preserves_other_fields_and_reminders(self):
        event_id = self.add_managed_event()
        before = self.reminder_snapshot()
        interaction = self.interaction()
        await self.edit(interaction, event_id, name=" Canyon ")
        stored, = self.stored_events()
        self.assertEqual((stored.name, stored.description, stored.starts_at),
                         ("Canyon", "Prepare", datetime(2026, 9, 25, 19)))
        self.assertEqual(self.reminder_snapshot(), before)
        self.assert_response(interaction,
            f"✅ Event `{event_id}` updated. Starts at `2026-09-25 17:00` Apocalypse Time.",
        )

    async def test_description_edit_and_clear_preserve_reminders(self):
        event_id = self.add_managed_event()
        before = self.reminder_snapshot()
        for value, expected in ((" New instructions ", "New instructions"), (" ", None)):
            with self.subTest(value=value):
                interaction = self.interaction()
                await self.edit(interaction, event_id, description=value)
                stored, = self.stored_events()
                self.assertEqual(stored.description, expected)
                self.assertEqual(stored.name, "Duel")
                self.assertEqual(stored.starts_at, datetime(2026, 9, 25, 19))
                self.assertEqual(self.reminder_snapshot(), before)

    async def test_at_reschedule_changes_utc_and_resets_only_target_reminders(self):
        event_id = self.add_managed_event()
        other_id = self.add_managed_event(2)
        other_before = [row for row in self.reminder_snapshot() if row.event_id == other_id]
        interaction = self.interaction()
        await self.edit(interaction, event_id, starts_at=" 2026-12-31 23:30 ")
        with self.sessions() as session:
            record = session.get(Event, event_id)
            self.assertEqual(record.starts_at, datetime(2027, 1, 1, 1, 30))
            self.assertIsNone(record.starts_at.tzinfo)
            self.assertEqual((record.name, record.description), ("Duel", "Prepare"))
            self.assertEqual(session.scalar(text("SELECT starts_at FROM events WHERE id = :id").bindparams(id=event_id)),
                             "2027-01-01 01:30:00.000000")
        self.assertEqual(self.reminder_snapshot(), other_before)
        self.assert_response(interaction,
            f"✅ Event `{event_id}` updated. Starts at `2026-12-31 23:30` Apocalypse Time.",
        )

    async def test_same_start_time_does_not_reset_reminders(self):
        event_id = self.add_managed_event()
        before = self.reminder_snapshot()
        await self.edit(self.interaction(), event_id, starts_at="2026-9-25 17:00", name="Renamed")
        self.assertEqual(self.reminder_snapshot(), before)

    async def test_edit_all_fields_together(self):
        event_id = self.add_managed_event()
        await self.edit(self.interaction(), event_id, name="New", description="Changed",
                        starts_at="2026-09-26 08:00")
        stored, = self.stored_events()
        self.assertEqual((stored.name, stored.description, stored.starts_at),
                         ("New", "Changed", datetime(2026, 9, 26, 10)))
        self.assertEqual(self.reminder_snapshot(), [])

    async def test_delete_uses_database_cascade_and_preserves_other_events(self):
        event_id = self.add_managed_event()
        other_id = self.add_managed_event(2)
        other_before = [row for row in self.reminder_snapshot() if row.event_id == other_id]
        interaction = self.interaction()
        await self.delete(interaction, event_id)
        self.assertEqual([item.id for item in self.stored_events()], [other_id])
        self.assertEqual(self.reminder_snapshot(), other_before)
        self.assert_response(interaction, f"✅ Event `{event_id}` deleted.")

    async def test_r4_r5_and_unlinked_admin_can_edit_and_delete(self):
        for user_id, admin in ((10, False), (20, False), (99, True)):
            with self.subTest(user_id=user_id):
                event_id = self.add_managed_event()
                await self.edit(self.interaction(user_id=user_id, admin=admin), event_id, name="New")
                self.assertEqual(self.stored_events()[0].name, "New")
                interaction = self.interaction(user_id=user_id, admin=admin)
                await self.delete(interaction, event_id)
                self.assert_response(interaction, f"✅ Event `{event_id}` deleted.")
                self.assertEqual(self.stored_events(), [])
                self.assertEqual(self.reminder_snapshot(), [])

    async def test_member_unlinked_and_foreign_officers_cannot_edit_or_delete(self):
        event_id = self.add_managed_event()
        before = self.reminder_snapshot()
        for command in (self.edit, self.delete):
            for user_id in (30, 99, 40, 50):
                with self.subTest(command=command.__name__, user_id=user_id):
                    interaction = self.interaction(user_id=user_id)
                    options = {"name": "Forbidden"} if command is self.edit else {}
                    await command(interaction, event_id, **options)
                    self.assert_response(interaction,
                        "❌ Event not found in this server, or you do not have permission to manage it.",
                    )
                    self.assertEqual(self.stored_events()[0].name, "Duel")
                    self.assertEqual(self.reminder_snapshot(), before)

    async def test_cross_alliance_and_guild_event_ids_are_isolated(self):
        bravo = self.add_managed_event(2)
        foreign = self.add_managed_event(3)
        before = self.reminder_snapshot()
        for command in (self.edit, self.delete):
            for event_id, admin in ((bravo, False), (foreign, False), (foreign, True)):
                with self.subTest(command=command.__name__, event_id=event_id, admin=admin):
                    interaction = self.interaction(admin=admin)
                    options = {"name": "Forbidden"} if command is self.edit else {}
                    await command(interaction, event_id, **options)
                    self.assert_response(interaction,
                        "❌ Event not found in this server, or you do not have permission to manage it.",
                    )
                    self.assertEqual(len(self.stored_events()), 2)
                    self.assertTrue(all(e.name == "Duel" for e in self.stored_events()))
                    self.assertEqual(self.reminder_snapshot(), before)

    async def test_foreign_officer_can_manage_own_same_named_alliance_only(self):
        local_id = self.add_managed_event()
        foreign_id = self.add_managed_event(3)
        await self.edit(self.interaction(user_id=50, guild_id=2002), foreign_id, name="Foreign edit")
        await self.delete(self.interaction(user_id=50, guild_id=2002), foreign_id)
        self.assertEqual([e.id for e in self.stored_events()], [local_id])
        self.assertEqual(len(self.reminder_snapshot()), 2)

    async def test_unknown_wrong_and_deleted_ids_are_safe(self):
        deleted_id = self.add_managed_event()
        await self.delete(self.interaction(), deleted_id)
        for command in (self.edit, self.delete):
            for event_id in (-1, 0, 999, deleted_id):
                interaction = self.interaction(admin=True)
                options = {"description": "Changed"} if command is self.edit else {}
                await command(interaction, event_id, **options)
                self.assert_response(interaction,
                    "❌ Event not found in this server, or you do not have permission to manage it.",
                )
        self.assertEqual(self.stored_events(), [])

    async def test_invalid_edit_is_atomic_and_keeps_reminders(self):
        event_id = self.add_managed_event()
        before = self.reminder_snapshot()
        for options, expected in (
            ({}, "❌ Provide at least one field to edit."),
            ({"name": " "}, "❌ Event name cannot be empty."),
            ({"name": "New", "starts_at": "2026-02-30 17:00"},
             "❌ Start time must use format `YYYY-MM-DD HH:MM`."),
        ):
            with self.subTest(options=options):
                interaction = self.interaction()
                await self.edit(interaction, event_id, **options)
                self.assert_response(interaction, expected)
                self.assertEqual(self.stored_events()[0].name, "Duel")
                self.assertEqual(self.reminder_snapshot(), before)

    async def test_edit_and_delete_require_a_server(self):
        for command in (self.edit, self.delete):
            interaction = self.interaction(guild_id=None)
            options = {"name": "New"} if command is self.edit else {}
            await command(interaction, 1, **options)
            self.assert_response(interaction,
                "❌ This command can only be used inside a Discord server.",
            )
