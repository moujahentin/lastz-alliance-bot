from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from lastz_bot.commands.event import setup_event_commands
from lastz_bot.database.base import Base
from lastz_bot.database.models import Alliance, Event, Guild, Member


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
        clock = patch("lastz_bot.commands.event.utc_now_naive", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)

        # Exercise the registered callbacks without connecting to Discord.
        tree = Mock()
        setup_event_commands(tree)
        group = tree.add_command.call_args.args[0]
        self.create = group.get_command("create").callback
        self.list_events = group.get_command("list").callback

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
                Member(alliance_id=1, game_name="Member", rank="MEMBER", discord_user_id=30),
                Member(alliance_id=2, game_name="Other officer", rank="R5", discord_user_id=40),
                Member(alliance_id=3, game_name="Foreign officer", rank="R5", discord_user_id=50),
                Member(alliance_id=1, game_name="Local member", rank="MEMBER", discord_user_id=50),
            ])
            session.commit()

    def interaction(self, user_id=10, guild_id=1001, admin=False):
        return SimpleNamespace(
            guild=SimpleNamespace(id=guild_id) if guild_id is not None else None,
            user=SimpleNamespace(
                id=user_id, guild_permissions=SimpleNamespace(administrator=admin),
            ),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

    def assert_response(self, interaction, message):
        interaction.response.send_message.assert_awaited_once_with(message, ephemeral=True)

    def stored_events(self):
        with self.sessions() as session:
            return session.scalars(select(Event).order_by(Event.id)).all()

    def add_event(self, alliance_id, name, starts_at, description=None):
        with self.sessions() as session:
            session.add(Event(
                alliance_id=alliance_id, name=name, starts_at=starts_at,
                description=description, created_by_discord_user_id=20,
            ))
            session.commit()

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
            "**Upcoming events for `Alpha`:**\n• `2026-09-25 17:00` AT — **Duel** — Prepare",
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
            "**Upcoming events for `Alpha`:**\n• `2026-12-31 23:30` AT — **Reset**",
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
            "• `2026-09-25 16:00` AT — **Now**\n"
            "• `2026-09-25 17:00` AT — **Later**",
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
