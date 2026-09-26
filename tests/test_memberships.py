"""Membership identity, management audit, and authorization under serialized writes."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event as Signal
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import event as sa_event, select, text
from sqlalchemy.exc import IntegrityError

from lastz_bot.commands.member import setup_member_commands
from lastz_bot.database.models import Member, MembershipChange, EventRSVP
from lastz_bot.event_management import EventManagementError
from lastz_bot.memberships import add_member, change_member, list_members
from lastz_bot.permissions import get_management_rank
from lastz_bot.rsvp import set_rsvp
import test_participation as previous


class MembershipTests(unittest.IsolatedAsyncioTestCase):
    connect = previous.ParticipationTests.connect
    rows = previous.ParticipationTests.rows
    interaction = previous.ParticipationTests.interaction
    once = previous.ParticipationTests.once

    def setUp(self):
        previous.ParticipationTests.setUp(self)
        for target in ('lastz_bot.commands.member.SessionLocal',):
            p = patch(target, self.sessions); p.start(); self.addCleanup(p.stop)
        p = patch('lastz_bot.memberships.utc_now_naive', side_effect=lambda: self.now)
        p.start(); self.addCleanup(p.stop)
        tree = Mock(); setup_member_commands(tree)
        self.commands_member = {c.name: c.callback for c in tree.add_command.call_args.args[0].commands}

    def change(self, user=30, actor=10, admin=False, alliance='Alpha', guild=1, **changes):
        return change_member(self.sessions, guild, alliance, actor, admin, discord_user_id=user, **changes)

    def member(self, user=30, alliance=1):
        with self.sessions() as session:
            return session.scalar(select(Member).where(Member.alliance_id == alliance, Member.discord_user_id == user))

    def history(self):
        with self.sessions() as session:
            return session.scalars(select(MembershipChange).order_by(MembershipChange.id)).all()

    def test_all_ranks_are_membership_scoped_and_database_constrained(self):
        for i, rank in enumerate(('R1','R2','R3','R4','R5')):
            add_member(self.sessions, 1, 'Alpha', 999, True, game_name=f'new{i}', rank=rank, discord_user_id=100+i)
        add_member(self.sessions, 1, 'Bravo', 999, True, game_name='same user', rank='R2', discord_user_id=103)
        self.assertEqual(self.member(103).rank, 'R4')
        self.assertEqual(self.member(103, 2).rank, 'R2')
        with self.assertRaises(IntegrityError), self.sessions() as session:
            session.add(Member(alliance_id=1, game_name='invalid', rank='MEMBER'))
            session.commit()
        self.assertEqual(len(self.history()), 6)

    def test_deactivate_reactivate_identity_rsvp_and_audit(self):
        occurrence = self.once(); original = self.member()
        set_rsvp(self.sessions, 1, occurrence, 30, 'going')
        self.change(active=False)
        self.assertFalse(self.member().active)
        with self.assertRaises(EventManagementError):
            set_rsvp(self.sessions, 1, occurrence, 30, 'maybe')
        self.change(active=True)
        self.assertEqual(self.member().id, original.id)
        with self.sessions() as session:
            self.assertEqual(session.get(EventRSVP, (occurrence,30)).response, 'going')
        self.assertEqual([(r.previous_active,r.new_active,r.actor_id,r.member_id,r.changed_at) for r in self.history()],
                         [(True,False,10,original.id,self.now),(False,True,10,original.id,self.now)])
        set_rsvp(self.sessions, 1, occurrence, 30, 'maybe')

    def test_effective_changes_only_include_self_changes(self):
        self.assertFalse(self.change(rank='R1', active=True))
        self.assertEqual(self.history(), [])
        self.change(rank='R3'); self.change(rank='R3')
        self.change(user=10, rank='R3')
        self.assertEqual([(r.previous_rank,r.new_rank,r.actor_id) for r in self.history()],
                         [('R1','R3',10),('R4','R3',10)])
        self.assertIsNone(get_management_rank(1,'Alpha',10))
        with self.assertRaises(EventManagementError): self.change(actor=10, rank='R2')
        self.change(user=10, actor=20, rank='R4')
        self.assertEqual(get_management_rank(1,'Alpha',10), 'R4')

    def test_inactive_r5_self_deactivation_no_last_leader_guard_admin_recovery(self):
        self.change(user=20, actor=20, active=False)
        self.assertIsNone(get_management_rank(1,'Alpha',20))
        with self.assertRaises(EventManagementError): self.change(actor=20, rank='R2')
        with self.assertRaises(EventManagementError): self.change(user=20, active=True)
        self.change(user=20, actor=999, admin=True, active=True)
        self.assertEqual(get_management_rank(1,'Alpha',20), 'R5')
        self.change(user=20, actor=20, rank='R1')
        self.assertEqual(self.member(20).rank, 'R1')

    def test_r4_can_deactivate_self_but_not_reactivate_without_admin(self):
        self.change(user=10, active=False)
        with self.assertRaises(EventManagementError): self.change(user=10, active=True)
        self.change(user=10, actor=999, admin=True, active=True)
        self.assertEqual(len(self.history()), 2)

    def test_target_rank_limits_members_unlinked_and_tenants(self):
        for kwargs in ({'user':20,'rank':'R3'}, {'rank':'R5'}, {'actor':30,'rank':'R2'},
                       {'actor':999,'rank':'R2'}, {'actor':40,'rank':'R2'},
                       {'user':50,'guild':2,'rank':'R2'}, {'user':40,'alliance':'Bravo','rank':'R2'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(EventManagementError): self.change(**kwargs)
        self.assertEqual(self.history(), [])
        self.change(actor=20, rank='R5')
        self.assertEqual(self.member().rank, 'R5')

    def test_inactive_identity_cannot_be_recreated_and_links_are_audited(self):
        self.change(active=False)
        with self.assertRaises(EventManagementError):
            add_member(self.sessions,1,'Alpha',10,False,game_name='replacement',discord_user_id=30)
        self.change(link_to=31)
        self.assertEqual(self.member(31).id, self.history()[0].member_id)
        self.assertEqual((self.history()[-1].previous_discord_user_id,self.history()[-1].new_discord_user_id),(30,31))

    async def test_member_commands_selections_roster_soft_remove_and_errors(self):
        target = SimpleNamespace(id=30)
        for command, options in [('rank',{'rank':'R3'}),('deactivate',{}),('activate',{})]:
            interaction = self.interaction()
            await self.commands_member[command](interaction, alliance='Alpha', member=target, **options)
            self.assertTrue(interaction.followup.send.call_args.kwargs['ephemeral'])
            self.assertIn('updated', interaction.followup.send.call_args.args[0])
        interaction = self.interaction()
        await self.commands_member['remove'](interaction,'Alpha','30')
        self.assertFalse(self.member().active)
        interaction = self.interaction()
        await self.commands_member['list'](interaction,'Alpha')
        display = interaction.followup.send.call_args.args[0]
        self.assertIn('R3 — inactive',display); self.assertIn('<@30>',display)
        interaction = self.interaction(actor=30)
        await self.commands_member['activate'](interaction,'Alpha',member=target)
        self.assertIn('does not permit',interaction.followup.send.call_args.args[0])
        interaction = self.interaction(guild=None)
        await self.commands_member['list'](interaction,'Alpha')
        self.assertIn('inside a Discord server',interaction.followup.send.call_args.args[0])

    def test_waiting_rsvp_observes_committed_deactivation(self):
        occurrence = self.once(); attempted = Signal()
        def before(connection,cursor,statement,parameters,context,executemany):
            if statement == 'BEGIN IMMEDIATE': attempted.set()
        # Hold the write lock while a second connection tries to RSVP.
        with self.sessions() as writer, ThreadPoolExecutor(max_workers=1) as pool:
            writer.execute(text('BEGIN IMMEDIATE'))
            writer.get(Member,self.member().id).active = False
            sa_event.listen(self.engine,'before_cursor_execute',before)
            future = pool.submit(set_rsvp,self.sessions,1,occurrence,30,'going')
            try:
                self.assertTrue(attempted.wait(3)); writer.commit()
                with self.assertRaises(EventManagementError): future.result(timeout=5)
            finally:
                writer.rollback(); sa_event.remove(self.engine,'before_cursor_execute',before)
        with self.sessions() as session: self.assertIsNone(session.get(EventRSVP,(occurrence,30)))

    def test_waiting_manager_observes_own_deactivation(self):
        attempted = Signal()
        def before(connection,cursor,statement,parameters,context,executemany):
            if statement == 'BEGIN IMMEDIATE': attempted.set()
        with self.sessions() as writer, ThreadPoolExecutor(max_workers=1) as pool:
            writer.execute(text('BEGIN IMMEDIATE'))
            writer.get(Member,self.member(10).id).active=False
            sa_event.listen(self.engine,'before_cursor_execute',before)
            future=pool.submit(self.change,rank='R3')
            try:
                self.assertTrue(attempted.wait(3)); writer.commit()
                with self.assertRaises(EventManagementError): future.result(timeout=5)
            finally:
                writer.rollback(); sa_event.remove(self.engine,'before_cursor_execute',before)
        self.assertEqual(self.member().rank,'R1'); self.assertEqual(self.history(),[])


    async def test_add_link_and_unlinked_game_name_commands_audit_and_retain_identity(self):
        interaction = self.interaction()
        await self.commands_member['add'](interaction, 'Alpha', 'Unlinked', rank='R2')
        with self.sessions() as session:
            original = session.scalar(select(Member).where(Member.game_name == 'Unlinked')).id
        interaction = self.interaction()
        await self.commands_member['rank'](interaction, 'Alpha', 'R3', game_name='Unlinked')
        interaction = self.interaction()
        await self.commands_member['link'](interaction, 'Alpha', 'Unlinked', SimpleNamespace(id=77))
        self.assertEqual(self.member(77).id, original)
        self.assertEqual(self.member(77).rank, 'R3')
        records = [row for row in self.history() if row.member_id == original]
        self.assertEqual(len(records), 3)
        self.assertEqual((records[0].previous_rank, records[0].new_rank, records[0].actor_id), (None, 'R2', 10))
        self.assertEqual((records[-1].previous_discord_user_id, records[-1].new_discord_user_id), (None, 77))
        interaction = self.interaction()
        await self.commands_member['rank'](interaction, 'Alpha', 'R4', member=SimpleNamespace(id=77), game_name='Unlinked')
        self.assertIn('not both', interaction.followup.send.call_args.args[0])
        self.assertEqual(len(self.history()), 3)

    def test_inactive_officer_promotion_does_not_reactivate_or_restore_access(self):
        self.change(active=False)
        self.change(actor=20, rank='R4')
        self.assertFalse(self.member().active)
        self.assertIsNone(get_management_rank(1, 'Alpha', 30))
        self.change(actor=20, active=True)
        self.assertEqual(get_management_rank(1, 'Alpha', 30), 'R4')
        self.assertEqual([(r.previous_rank, r.new_rank, r.previous_active, r.new_active) for r in self.history()],
                         [('R1', 'R1', True, False), ('R1', 'R4', False, False), ('R4', 'R4', False, True)])

    def test_missing_or_cross_guild_membership_and_duplicate_link_leave_no_audit(self):
        for kwargs in ({'user':999, 'rank':'R2'}, {'user':30, 'guild':2, 'admin':True, 'rank':'R2'},
                       {'user':30, 'link_to':10}):
            with self.subTest(kwargs=kwargs), self.assertRaises(EventManagementError):
                self.change(**kwargs)
        self.assertEqual(self.history(), [])
        self.assertEqual(self.member().rank, 'R1')
