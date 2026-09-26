from interaction_fakes import transport
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from lastz_bot.commands.alliance import setup_alliance_commands
from lastz_bot.database.base import Base
from lastz_bot.database.models import Alliance, Guild, Member


class AllianceChannelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        self.sessions = sessionmaker(bind=engine)
        for target in ("lastz_bot.commands.alliance.SessionLocal", "lastz_bot.permissions.SessionLocal"):
            patcher = patch(target, self.sessions)
            patcher.start()
            self.addCleanup(patcher.stop)
        tree = Mock()
        setup_alliance_commands(tree)
        self.command = tree.add_command.call_args.args[0].get_command("set-channel").callback
        with self.sessions() as session:
            session.add_all([Guild(id=1, name="One"), Guild(id=2, name="Two")])
            session.flush()
            session.add_all([
                Alliance(id=1, guild_id=1, name="Alpha"),
                Alliance(id=2, guild_id=1, name="Bravo"),
                Alliance(id=3, guild_id=2, name="Alpha"),
            ])
            session.flush()
            for alliance, user, rank in ((1, 10, "R4"), (1, 20, "R5"), (1, 30, "R1"), (2, 40, "R5"), (3, 50, "R5"), (1, 50, "R1")):
                session.add(Member(alliance_id=alliance, game_name=str(user), discord_user_id=user, rank=rank))
            session.commit()

    def interaction(self, user=10, guild=1, admin=False):
        return SimpleNamespace(
            guild=SimpleNamespace(id=guild, me=Mock()) if guild is not None else None,
            user=SimpleNamespace(id=user, guild_permissions=SimpleNamespace(administrator=admin)),
            **transport(),
        )

    def channel(self, channel_id=101, guild=1, view=True, send=True):
        return SimpleNamespace(
            id=channel_id, guild=SimpleNamespace(id=guild), mention=f"<#{channel_id}>",
            permissions_for=Mock(return_value=SimpleNamespace(view_channel=view, send_messages=send)),
        )

    def configuration(self):
        with self.sessions() as session:
            return dict(session.execute(select(Alliance.id, Alliance.reminder_channel_id)).all())

    async def test_r4_r5_and_admin_can_persist_and_replace_channel(self):
        for user, admin, channel_id in ((10, False, 101), (20, False, 102), (99, True, 103)):
            with self.subTest(user=user):
                interaction = self.interaction(user=user, admin=admin)
                await self.command(interaction, " Alpha ", self.channel(channel_id))
                interaction.followup.send.assert_awaited_once_with(
                    f"✅ Event reminders for alliance `Alpha` will be sent to <#{channel_id}>.", ephemeral=True,
                )
                self.assertEqual(self.configuration(), {1: channel_id, 2: None, 3: None})

    async def test_member_unlinked_and_other_tenant_officers_are_denied(self):
        for user, guild, alliance in ((30, 1, "Alpha"), (99, 1, "Alpha"), (40, 1, "Alpha"), (50, 1, "Alpha"), (10, 1, "Bravo"), (10, 2, "Alpha")):
            with self.subTest(user=user, guild=guild, alliance=alliance):
                interaction = self.interaction(user=user, guild=guild)
                await self.command(interaction, alliance, self.channel(guild=guild))
                interaction.followup.send.assert_awaited_once_with(
                    "❌ You need to be an R4, R5, or Server Administrator of this alliance to configure event reminders.",
                    ephemeral=True,
                )
                self.assertEqual(self.configuration(), {1: None, 2: None, 3: None})

    async def test_same_named_alliance_in_other_guild_updates_only_itself(self):
        interaction = self.interaction(user=50, guild=2)
        await self.command(interaction, "Alpha", self.channel(201, guild=2))
        self.assertEqual(self.configuration(), {1: None, 2: None, 3: 201})

    async def test_cross_guild_channel_is_rejected_even_for_admin(self):
        interaction = self.interaction(admin=True)
        await self.command(interaction, "Alpha", self.channel(201, guild=2))
        interaction.followup.send.assert_awaited_once_with(
            "❌ Choose a channel in this Discord server.", ephemeral=True,
        )
        self.assertEqual(self.configuration(), {1: None, 2: None, 3: None})

    async def test_bot_needs_view_and_send_permissions(self):
        for view, send in ((False, True), (True, False)):
            interaction = self.interaction()
            await self.command(interaction, "Alpha", self.channel(view=view, send=send))
            interaction.followup.send.assert_awaited_once_with(
                "❌ I need View Channel and Send Messages permissions in that channel.", ephemeral=True,
            )
        self.assertEqual(self.configuration(), {1: None, 2: None, 3: None})

    async def test_guild_and_alliance_validation(self):
        for guild, alliance, expected in (
            (None, "Alpha", "❌ This command can only be used inside a Discord server."),
            (1, " ", "❌ Alliance name cannot be empty."),
            (99, "Alpha", "❌ This Discord server has not been initialized yet. Run `/setup` first."),
            (1, "Missing", "❌ Alliance `Missing` does not exist."),
        ):
            with self.subTest(guild=guild, alliance=alliance):
                interaction = self.interaction(guild=guild, admin=True)
                await self.command(interaction, alliance, self.channel(guild=guild))
                interaction.followup.send.assert_awaited_once_with(expected, ephemeral=True)
        self.assertEqual(self.configuration(), {1: None, 2: None, 3: None})


    async def test_deactivation_between_preflight_and_write_blocks_channel_change(self):
        channel = self.channel()
        def deactivate_during_permission_check(bot_member):
            with self.sessions() as session:
                member = session.scalar(select(Member).where(Member.alliance_id == 1, Member.discord_user_id == 10))
                member.active = False
                session.commit()
            return SimpleNamespace(view_channel=True, send_messages=True)
        channel.permissions_for.side_effect = deactivate_during_permission_check
        interaction = self.interaction()
        await self.command(interaction, 'Alpha', channel)
        self.assertIn('no longer', interaction.followup.send.call_args.args[0])
        self.assertEqual(self.configuration(), {1: None, 2: None, 3: None})
        await self.command(self.interaction(admin=True), 'Alpha', self.channel())
        self.assertEqual(self.configuration(), {1: 101, 2: None, 3: None})
