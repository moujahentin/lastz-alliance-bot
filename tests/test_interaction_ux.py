"""Discord transport failure must never retry an already committed mutation."""
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from threading import Event as Signal
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord import app_commands
from sqlalchemy.exc import OperationalError
from sqlalchemy import event as sa_event, text

from lastz_bot.commands.general import setup_general_commands
from lastz_bot.commands.member_context import setup_member_context_commands, MemberPanel
from lastz_bot.database.models import Alliance, Guild, Member
from lastz_bot.event_management import EventManagementError
from lastz_bot.interactions import acknowledge, respond
from lastz_bot.memberships import add_member, change_member, linked_memberships
import test_memberships as previous


class InteractionUXTests(unittest.IsolatedAsyncioTestCase):
    connect = previous.MembershipTests.connect
    rows = previous.MembershipTests.rows
    once = previous.MembershipTests.once
    member = previous.MembershipTests.member
    history = previous.MembershipTests.history
    change = previous.MembershipTests.change

    def setUp(self):
        previous.MembershipTests.setUp(self)
        p = patch('lastz_bot.commands.member_context.SessionLocal', self.sessions)
        p.start(); self.addCleanup(p.stop)
        self.client = discord.Client(intents=discord.Intents.none())
        self.tree = app_commands.CommandTree(self.client)
        setup_member_context_commands(self.tree)
        self.info = self.tree.get_command('Alliance Member Info', type=discord.AppCommandType.user).callback
        self.manage = self.tree.get_command('Alliance Member Manage', type=discord.AppCommandType.user).callback

    def interaction(self, actor=10, guild=1, admin=False):
        result = previous.MembershipTests.interaction(self, actor, guild, admin)
        result.id, result.guild_id = 123, guild
        return result

    def failure(self):
        return discord.NotFound(SimpleNamespace(status=404, reason='Not Found'),
                                {'code': 10062, 'message': 'Unknown interaction'})

    def panel(self, actor=10, target=30, admin=False, selected=True):
        rows = linked_memberships(self.sessions, 1, target, actor_id=actor, administrator=admin)
        return MemberPanel(actor, 1, target, rows, selected=rows[0] if selected else None)

    async def test_acknowledged_before_member_write_and_exactly_one_private_confirmation(self):
        interaction = self.interaction()
        def mutation(*args, **kwargs):
            interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
            return change_member(*args, **kwargs)
        with patch('lastz_bot.commands.member.change_member', side_effect=mutation) as write:
            await self.commands_member['rank'](interaction, 'Alpha', 'R3', member=SimpleNamespace(id=30))
        write.assert_called_once()
        interaction.response.send_message.assert_not_awaited()
        interaction.followup.send.assert_awaited_once_with('✅ Membership updated.', ephemeral=True)
        self.assertEqual(len(self.history()), 1)

    async def test_business_error_after_defer_is_private_and_no_write(self):
        interaction = self.interaction(actor=30)
        await self.commands_member['rank'](interaction, 'Alpha', 'R3', member=SimpleNamespace(id=30))
        interaction.response.defer.assert_awaited_once()
        self.assertIn('does not permit', interaction.followup.send.call_args.args[0])
        self.assertTrue(interaction.followup.send.call_args.kwargs['ephemeral'])
        self.assertEqual(self.history(), [])

    async def test_unknown_confirmation_after_commit_logs_without_retry_or_rollback(self):
        interaction = self.interaction()
        interaction.followup.send.side_effect = self.failure()
        with self.assertLogs('lastz_bot.interactions', level='WARNING') as logs:
            await self.commands_member['rank'](interaction, 'Alpha', 'R3', member=SimpleNamespace(id=30))
        self.assertEqual(self.member().rank, 'R3')
        self.assertEqual(len(self.history()), 1)
        interaction.followup.send.assert_awaited_once()
        self.assertIn('operation not retried', logs.output[0])
        self.assertIn('10062', logs.output[0])

    async def test_failed_acknowledgement_prevents_mutation(self):
        interaction = self.interaction()
        interaction.response.defer.side_effect = self.failure()
        with self.assertLogs('lastz_bot.interactions', level='WARNING'):
            await self.commands_member['rank'](interaction, 'Alpha', 'R3', member=SimpleNamespace(id=30))
        self.assertEqual(self.member().rank, 'R1')
        self.assertEqual(self.history(), [])
        interaction.followup.send.assert_not_awaited()

    async def test_already_acknowledged_never_sends_second_initial(self):
        interaction = self.interaction()
        await interaction.response.send_message('Initial', ephemeral=True)
        self.assertTrue(await acknowledge(interaction))
        await respond(interaction, 'Followup')
        interaction.response.defer.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()
        interaction.followup.send.assert_awaited_once_with('Followup', ephemeral=True)

    async def test_database_error_is_private_without_internal_details(self):
        interaction = self.interaction()
        with patch('lastz_bot.commands.member.change_member', side_effect=OperationalError('private SQL', {}, Exception('secret'))):
            with self.assertLogs('lastz_bot.interactions', level='ERROR') as logs:
                await self.commands_member['rank'](interaction, 'Alpha', 'R3', member=SimpleNamespace(id=30))
        self.assertIn('Check the current state', interaction.followup.send.call_args.args[0])
        self.assertNotIn('private SQL', str(logs.output))
        self.assertNotIn('secret', str(interaction.followup.send.call_args))

    async def test_ping_stays_immediate_public(self):
        tree = Mock()
        setup_general_commands(tree, SimpleNamespace(latency=.123))
        interaction = self.interaction()
        await tree.command.return_value.call_args.args[0](interaction)
        interaction.response.send_message.assert_awaited_once_with('🏓 Pong! `123 ms`')
        interaction.response.defer.assert_not_awaited()
        interaction.followup.send.assert_not_awaited()

    async def test_info_preserves_roster_visibility_even_for_unlinked_reader(self):
        interaction = self.interaction(actor=999)
        await self.info(interaction, SimpleNamespace(id=30))
        self.assertIn('30 — Alpha — R1 — active — linked <@30>', interaction.followup.send.call_args.args[0])
        self.assertTrue(interaction.followup.send.call_args.kwargs['ephemeral'])
        self.assertEqual(self.history(), [])

    async def test_info_no_link_and_foreign_guild_never_exposed(self):
        for target in (999, 40):
            interaction = self.interaction(guild=2)
            await self.info(interaction, SimpleNamespace(id=target))
            self.assertIn('No linked', interaction.followup.send.call_args.args[0])
        interaction = self.interaction(guild=1)
        await self.info(interaction, SimpleNamespace(id=50))
        self.assertIn('R1', interaction.followup.send.call_args.args[0])
        self.assertNotIn('R5', interaction.followup.send.call_args.args[0])

    async def test_info_multiple_memberships_are_sorted_and_inactive_visible(self):
        add_member(self.sessions, 1, 'Bravo', 999, True, game_name='Second', discord_user_id=30)
        self.change(active=False)
        interaction = self.interaction(actor=30)
        await self.info(interaction, SimpleNamespace(id=30))
        message = interaction.followup.send.call_args.args[0]
        self.assertLess(message.index('Alpha'), message.index('Bravo'))
        self.assertIn('inactive', message)

    async def test_manage_open_requires_authority_and_never_mutates(self):
        for actor in (30, 999, 40):
            interaction = self.interaction(actor=actor)
            await self.manage(interaction, SimpleNamespace(id=30))
            self.assertNotIn('view', interaction.followup.send.call_args.kwargs)
        interaction = self.interaction()
        await self.manage(interaction, SimpleNamespace(id=30))
        panel = interaction.followup.send.call_args.kwargs['view']
        self.assertIsNone(panel.selected)
        self.assertEqual(panel.children[0].options[0].label, 'Alpha — 30')
        self.assertEqual(self.history(), [])
        panel.stop()

    async def test_explicit_multiple_selection_and_correct_target(self):
        add_member(self.sessions, 1, 'Bravo', 999, True, game_name='Other', discord_user_id=30)
        panel = self.panel(actor=999, admin=True, selected=False)
        self.assertEqual([o.label for o in panel.children[0].options], ['Alpha — 30', 'Bravo — Other'])
        interaction = self.interaction(actor=999, admin=True)
        await panel.choose(interaction, panel.rows[1].member_id)
        chosen = interaction.followup.send.call_args.kwargs['view']
        self.assertEqual(chosen.selected.alliance, 'Bravo')
        await chosen.apply(self.interaction(actor=999, admin=True), rank='R2')
        self.assertEqual(self.member(30, 2).rank, 'R2')
        self.assertEqual(self.member().rank, 'R1')

    async def test_native_rank_options_and_shared_mutation(self):
        panel = self.panel()
        self.assertEqual([o.value for o in panel.children[0].options], ['R1','R2','R3','R4','R5'])
        with patch('lastz_bot.commands.member_context.change_member', wraps=change_member) as write:
            await panel.apply(self.interaction(), rank='R3')
        write.assert_called_once()
        self.assertEqual(self.member().rank, 'R3')
        self.assertEqual(len(self.history()), 1)
        self.assertTrue(panel.is_finished())

    async def test_r4_cannot_promote_to_r5_and_r5_or_admin_can(self):
        interaction = self.interaction()
        await self.panel().apply(interaction, rank='R5')
        self.assertIn('does not permit', interaction.followup.send.call_args.args[0])
        for actor, admin, rank in ((20,False,'R5'), (999,True,'R1')):
            await self.panel(actor=actor,admin=admin).apply(self.interaction(actor=actor,admin=admin), rank=rank)
            self.assertEqual(self.member().rank,rank)

    async def test_authorization_revoked_since_open(self):
        panel = self.panel()
        self.change(user=10, actor=20, active=False)
        interaction = self.interaction()
        await panel.apply(interaction, rank='R3')
        self.assertIn('does not permit', interaction.followup.send.call_args.args[0])
        self.assertEqual(self.member().rank, 'R1')

    async def test_stale_rank_or_state_or_link_rejected_without_new_audit(self):
        for changes in ({'rank':'R2'}, {'active':False}, {'link_to':31}):
            panel = self.panel()
            self.change(**changes)
            count = len(self.history())
            interaction = self.interaction()
            await panel.apply(interaction, rank='R3')
            self.assertIn('❌', interaction.followup.send.call_args.args[0])
            self.assertEqual(len(self.history()), count)
            if 'link_to' in changes:
                self.change(user=31, link_to=30)
            self.change(rank='R1', active=True)

    async def test_activation_offers_explicit_opposite_and_stale_click_does_not_toggle(self):
        panel = self.panel()
        self.assertEqual(panel.children[1].label, 'Deactivate')
        await panel.children[1].callback(self.interaction())
        self.assertFalse(self.member().active)
        panel = self.panel()
        self.assertEqual(panel.children[1].label, 'Activate')
        self.change(active=True)
        count = len(self.history())
        await panel.children[1].callback(self.interaction())
        self.assertTrue(self.member().active)
        self.assertEqual(len(self.history()), count)

    async def test_other_actor_and_other_guild_cannot_hijack_panel(self):
        for interaction in (self.interaction(actor=20), self.interaction(guild=2,admin=True)):
            panel = self.panel()
            await panel.apply(interaction, rank='R3')
        self.assertEqual(self.history(), [])
        self.assertEqual(self.member().rank, 'R1')

    async def test_guessed_selection_and_deleted_membership_fail_safely(self):
        panel = self.panel(selected=False)
        interaction = self.interaction()
        await panel.choose(interaction, self.member(50,3).id)
        self.assertNotIn('view', interaction.followup.send.call_args.kwargs)
        selected = self.panel()
        with self.sessions() as session:
            session.delete(session.get(Member, selected.selected.member_id)); session.commit()
        await selected.apply(self.interaction(), rank='R2')
        self.assertEqual(self.history(), [])

    async def test_timeout_disables_components_and_rejects_stale_callbacks(self):
        panel = self.panel()
        panel.message = SimpleNamespace(edit=AsyncMock())
        await panel.on_timeout()
        self.assertTrue(all(c.disabled for c in panel.children))
        panel.message.edit.assert_awaited_once()
        await panel.apply(self.interaction(), rank='R2')
        self.assertEqual(self.history(), [])

    async def test_self_deactivation_closes_panel_and_admin_can_restore(self):
        panel = self.panel(target=10)
        await panel.apply(self.interaction(), active=False)
        self.assertFalse(self.member(10).active)
        await self.panel(actor=999,target=10,admin=True).apply(self.interaction(actor=999,admin=True),active=True)
        self.assertTrue(self.member(10).active)

    async def test_panel_confirmation_failure_leaves_exactly_one_audit(self):
        interaction = self.interaction()
        interaction.followup.send.side_effect = self.failure()
        with self.assertLogs('lastz_bot.interactions', level='WARNING'):
            await self.panel().apply(interaction, active=False)
        self.assertFalse(self.member().active)
        self.assertEqual(len(self.history()), 1)

    async def test_panel_cleanup_failure_does_not_hide_committed_result(self):
        panel = self.panel()
        panel.message = SimpleNamespace(edit=AsyncMock(side_effect=self.failure()))
        interaction = self.interaction()
        with self.assertLogs('lastz_bot.interactions', level='WARNING'):
            await panel.apply(interaction, rank='R2')
        self.assertIn('Membership updated', interaction.followup.send.call_args.args[0])
        self.assertEqual(len(self.history()), 1)

    async def test_panel_noop_has_no_audit_and_cannot_be_replayed(self):
        panel = self.panel()
        await panel.apply(self.interaction(), rank='R1')
        self.assertTrue(panel.is_finished())
        await panel.apply(self.interaction(), rank='R2')
        self.assertEqual(self.history(), [])
        self.assertEqual(self.member().rank, 'R1')

    async def test_manage_filters_r5_target_and_inactive_officer_but_admin_recovers(self):
        interaction = self.interaction()
        await self.manage(interaction, SimpleNamespace(id=20))
        self.assertNotIn('view', interaction.followup.send.call_args.kwargs)
        self.change(user=10, active=False)
        interaction = self.interaction()
        await self.manage(interaction, SimpleNamespace(id=30))
        self.assertNotIn('view', interaction.followup.send.call_args.kwargs)
        interaction = self.interaction(actor=999,admin=True)
        await self.manage(interaction, SimpleNamespace(id=10))
        panel = interaction.followup.send.call_args.kwargs['view']
        self.assertFalse(panel.rows[0].active)
        panel.stop()

    async def test_selection_pagination_and_info_are_bounded(self):
        with self.sessions() as session:
            for i in range(30):
                alliance = Alliance(guild_id=1,name=f'Extra{i:02}')
                session.add(alliance); session.flush()
                session.add(Member(alliance_id=alliance.id,game_name='😀'*100,discord_user_id=30))
            session.commit()
        panel = self.panel(actor=999,admin=True,selected=False)
        self.assertEqual(len(panel.children[0].options), 25)
        interaction = self.interaction(actor=999,admin=True)
        await panel.navigate(interaction, 1)
        next_panel = interaction.followup.send.call_args.kwargs['view']
        self.assertEqual(len(next_panel.children[0].options), 6)
        next_panel.stop()
        interaction = self.interaction()
        await self.info(interaction, SimpleNamespace(id=30))
        self.assertGreater(interaction.followup.send.await_count, 1)
        for call in interaction.followup.send.call_args_list:
            self.assertLess(len(call.args[0].encode('utf-16-le')) // 2, 2000)
            self.assertTrue(call.kwargs['ephemeral'])

    def test_snapshot_rechecked_after_waiting_for_write_lock(self):
        snapshot = linked_memberships(self.sessions,1,30)[0]
        attempted = Signal()
        def before(connection,cursor,statement,parameters,context,executemany):
            if statement == 'BEGIN IMMEDIATE': attempted.set()
        with self.sessions() as writer, ThreadPoolExecutor(max_workers=1) as pool:
            writer.execute(text('BEGIN IMMEDIATE'))
            writer.get(Member,snapshot.member_id).active=False
            sa_event.listen(self.engine,'before_cursor_execute',before)
            pending = pool.submit(change_member,self.sessions,1,'Alpha',10,False,
                                  discord_user_id=30,expected=snapshot,active=False)
            try:
                self.assertTrue(attempted.wait(3)); writer.commit()
                with self.assertRaisesRegex(EventManagementError,'changed while'):
                    pending.result(timeout=5)
            finally:
                writer.rollback(); sa_event.remove(self.engine,'before_cursor_execute',before)
        self.assertEqual(self.history(), [])

    async def test_setup_acknowledges_before_database_and_confirm_failure_does_not_repeat(self):
        from lastz_bot.commands.setup import setup_setup_commands
        tree = Mock(); setup_setup_commands(tree)
        command = tree.command.return_value.call_args.args[0]
        interaction = self.interaction(guild=99,admin=True)
        interaction.guild.name = 'New server'
        interaction.followup.send.side_effect = self.failure()
        def sessions():
            interaction.response.defer.assert_awaited_once()
            return self.sessions()
        with patch('lastz_bot.commands.setup.SessionLocal',side_effect=sessions) as factory:
            with self.assertLogs('lastz_bot.interactions',level='WARNING'):
                await command(interaction)
        factory.assert_called_once()
        with self.sessions() as session:
            self.assertEqual(session.get(Guild,99).name,'New server')

    async def test_registration_once_slash_and_autocomplete_preserved(self):
        from lastz_bot.main import LastZBot
        bot = LastZBot()
        setup_member_context_commands(bot.tree)
        self.assertEqual(len(bot.tree.get_commands(type=discord.AppCommandType.user)), 2)
        self.assertEqual({c.name for c in bot.tree.get_commands(type=discord.AppCommandType.chat_input)},
                         {'alliance','event','member','setup','ping'})
        interaction = self.interaction()
        create = bot.tree.get_command('event').get_command('create')
        choices = await create._params['audience'].autocomplete(interaction, 'R4')
        self.assertTrue(choices)
        interaction.response.defer.assert_not_awaited()
        interaction.followup.send.assert_not_awaited()
        await bot.close()
