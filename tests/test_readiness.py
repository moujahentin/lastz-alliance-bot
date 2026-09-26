"""Readiness is current, tenant-scoped intention, never attendance or a write."""
from datetime import timedelta
import re
import unittest
from unittest.mock import patch

from sqlalchemy import select, text

from lastz_bot.database.models import Alliance, Event, EventRSVP, Member
from lastz_bot.event_management import EventManagementError, _managed_event, delete_event, edit_event
from lastz_bot.memberships import change_member
from lastz_bot.nonresponders import nonresponders
from lastz_bot.readiness import get_readiness
from lastz_bot.readiness_output import readiness_page
from lastz_bot.recurrence import edit_series, stop_series
from lastz_bot.rsvp import get_rsvps, set_rsvp
import test_participation as participation


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    connect = participation.ParticipationTests.connect
    rows = participation.ParticipationTests.rows
    interaction = participation.ParticipationTests.interaction
    once = participation.ParticipationTests.once
    series = participation.ParticipationTests.series
    snapshot = participation.ParticipationTests.snapshot

    def setUp(self):
        participation.ParticipationTests.setUp(self)

    def roster(self, occurrence, actor=10, guild=1, admin=False):
        result = get_readiness(self.sessions, guild, occurrence, actor, admin)
        self.assertEqual(result.eligible, result.responded + result.no_response)
        self.assertEqual(result.responded, sum(len(result.groups[key]) for key in ('going', 'maybe', 'not_going')))
        return result

    def respond(self, occurrence, actor, response):
        set_rsvp(self.sessions, 1, occurrence, actor, response)

    def change(self, user=30, **values):
        change_member(self.sessions, 1, 'Alpha', 999, True, discord_user_id=user, **values)

    def edit(self, occurrence, **values):
        edit_event(self.sessions, 1, occurrence, 10, False, **values)

    def add_unlinked(self, name='Unlinked', rank='R1'):
        with self.sessions() as session:
            member = Member(alliance_id=1, game_name=name, rank=rank)
            session.add(member); session.commit()
            return member.id

    def test_basic_groups_totals_percentage_and_linked_unlinked(self):
        event = self.once('required'); self.add_unlinked()
        for actor, response in ((10, 'going'), (20, 'maybe'), (30, 'not_going')):
            self.respond(event, actor, response)
        roster = self.roster(event)
        self.assertEqual((roster.eligible, roster.responded, roster.no_response, roster.percentage), (5, 3, 2, 60.0))
        self.assertEqual([len(roster.groups[k]) for k in ('going', 'maybe', 'not_going', None)], [1, 1, 1, 2])
        rendered = readiness_page(roster)
        self.assertIn('Responded: 3/5 (60.0%)', rendered)
        self.assertIn('50 — <@50> (linked)', rendered)
        self.assertIn('Unlinked — unlinked', rendered)
        self.assertIn('intention, not attendance', rendered)

    def test_exact_r4_r5_audience_excludes_r3_and_other_ranks(self):
        event = self.once(); self.edit(event, audience='R4,R5'); self.change(rank='R3')
        roster = self.roster(event)
        self.assertEqual([m.discord_user_id for m in roster.members], [10, 20])
        self.assertEqual(roster.eligible, 2)

    def test_inactive_excluded_and_stale_response_does_not_inflate_counts(self):
        event = self.once(); self.respond(event, 30, 'going'); self.change(active=False)
        result = self.roster(event)
        self.assertEqual((result.eligible, result.responded, result.no_response), (3, 0, 3))
        # The old RSVP summary intentionally still exposes retained intentions.
        self.assertEqual(get_rsvps(self.sessions, 1, event, 10, False).groups['going'], (30,))
        self.change(active=True)
        self.assertEqual((self.roster(event).eligible, self.roster(event).responded), (4, 1))

    def test_rank_into_and_out_of_audience_uses_current_state(self):
        event = self.once(); self.edit(event, audience='R4,R5')
        self.change(rank='R4'); self.respond(event, 30, 'going')
        self.assertEqual((self.roster(event).eligible, self.roster(event).responded), (3, 1))
        self.change(rank='R3')
        self.assertEqual((self.roster(event).eligible, self.roster(event).responded), (2, 0))
        self.change(rank='R4')
        self.assertEqual(self.roster(event).responded, 1)

    def test_audience_edits_filter_retained_responses(self):
        event = self.once(); self.respond(event, 30, 'going')
        before = self.snapshot()
        self.edit(event, audience='R4')
        self.assertEqual((self.roster(event).eligible, self.roster(event).responded), (1, 0))
        self.edit(event, audience='Everyone')
        self.assertEqual(self.roster(event).responded, 1)
        self.assertEqual(self.snapshot(), before)

    def test_response_transitions_and_percentage(self):
        event = self.once()
        self.assertEqual(self.roster(event).no_response, 4)
        for response in ('going', 'maybe', 'not_going'):
            self.respond(event, 30, response)
            roster = self.roster(event)
            self.assertEqual((roster.responded, roster.no_response, roster.percentage), (1, 3, 25.0))
            self.assertEqual([m.discord_user_id for m in roster.groups[response]], [30])

    def test_zero_eligible_is_safe_even_for_administrator(self):
        event = self.once(); self.edit(event, audience='R2')
        roster = self.roster(event, actor=999, admin=True)
        self.assertEqual((roster.eligible, roster.responded, roster.no_response, roster.percentage), (0, 0, 0, 0.0))
        self.assertIn('Responded: 0/0 (0.0%)', readiness_page(roster))
        self.assertIn('No currently eligible members', readiness_page(roster))

    def test_management_authorization_including_officer_outside_audience(self):
        event = self.once(); self.edit(event, audience='R1')
        for actor, admin in ((10, False), (20, False), (999, True)):
            self.assertEqual(self.roster(event, actor=actor, admin=admin).eligible, 2)
        for actor in (30, 50, 999, 40):
            with self.assertRaises(EventManagementError): self.roster(event, actor=actor)
        self.change(user=10, active=False)
        with self.assertRaises(EventManagementError): self.roster(event)
        self.assertEqual(self.roster(event, admin=True).eligible, 2)

    def test_guessed_cross_guild_and_other_alliance_ids_return_same_safe_error(self):
        first = self.once(); other = self.once(alliance_id=2); foreign = self.once(alliance_id=3)
        messages = []
        for event, actor, guild, admin in ((first, 50, 2, True), (other, 10, 1, False),
                                           (foreign, 999, 1, True), (9999, 999, 1, True)):
            with self.assertRaises(EventManagementError) as caught:
                self.roster(event, actor=actor, guild=guild, admin=admin)
            messages.append(str(caught.exception))
        self.assertEqual(len(set(messages)), 1)
        self.assertNotIn('Duel', messages[0])
        self.assertEqual(self.roster(foreign, actor=50, guild=2).alliance_id, 3)

    def test_weekly_occurrence_stop_and_schedule_version_history(self):
        series = self.series('required'); occurrence = self.rows(series)[0].id
        self.respond(occurrence, 30, 'maybe')
        edit_series(self.sessions, 1, series, 10, False, time_at='18:00', now=self.now)
        self.assertEqual(self.roster(occurrence).groups['maybe'][0].discord_user_id, 30)
        self.assertEqual(self.roster(occurrence).status, 'cancelled')
        newer = next(e.id for e in self.rows(series) if e.status == 'scheduled')
        self.assertEqual(self.roster(newer).responded, 0)
        stop_series(self.sessions, 1, series, 10, False, now=self.now)
        self.assertEqual(self.roster(newer).status, 'stopped')

    def test_deleted_occurrence_denied_and_no_generation_on_view(self):
        event = self.once(); delete_event(self.sessions, 1, event, 10, False)
        with self.assertRaises(EventManagementError): self.roster(event)
        series = self.series(start=self.start-timedelta(days=700))
        before = [e.id for e in self.rows(series)]
        self.roster(before[0])
        self.assertEqual([e.id for e in self.rows(series)], before)

    def test_expired_occurrence_uses_current_eligibility_without_attendance(self):
        event = self.once(); self.respond(event, 30, 'going')
        self.now = self.start+timedelta(days=1)
        self.change(active=False)
        self.assertEqual((self.roster(event).responded, self.roster(event).eligible), (0, 3))
        with self.sessions() as session:
            self.assertEqual(session.get(Event, event).status, 'scheduled')

    def test_optional_none_and_deadline_do_not_change_intentions(self):
        event = self.once('required'); self.respond(event, 30, 'going')
        self.edit(event, rsvp_deadline='2026-09-22 16:00')
        snapshot = self.snapshot()
        for mode in ('required', 'optional', 'none'):
            self.edit(event, participation=mode)
            roster = self.roster(event)
            self.assertEqual((roster.eligible, roster.responded, roster.no_response), (4, 1, 3))
            if mode != 'required':
                self.assertIn('not a requirement', readiness_page(roster))
            if mode == 'none': self.assertIn('RSVP is disabled', readiness_page(roster))
        self.assertEqual(self.snapshot(), snapshot)

    def test_current_link_mapping_does_not_transfer_old_rsvp(self):
        event = self.once(); self.respond(event, 30, 'going'); self.add_unlinked()
        self.change(link_to=333)
        self.assertEqual((self.roster(event).eligible, self.roster(event).responded), (5, 0))
        self.respond(event, 333, 'maybe')
        self.assertEqual(self.roster(event).responded, 1)
        self.assertEqual(len(self.snapshot()), 2)

    def test_required_nonresponders_match_pr9_after_policy_changes(self):
        event = self.once('required'); self.add_unlinked()
        self.respond(event, 20, 'not_going'); self.respond(event, 30, 'maybe')
        for change in ({}, {'active': False}, {'active': True, 'rank': 'R3'}):
            if change: self.change(**change)
            roster = self.roster(event)
            with self.sessions() as session:
                expected = nonresponders(session, session.get(Event, event), 1)
            self.assertEqual([(m.member_id, m.discord_user_id) for m in roster.groups[None]],
                             [(m.member_id, m.discord_user_id) for m in expected])
        self.edit(event, participation='optional')
        with self.sessions() as session:
            self.assertEqual(nonresponders(session, session.get(Event, event), 1), ())

    def test_read_snapshot_is_coherent_during_membership_change(self):
        with self.engine.connect() as connection:
            connection.exec_driver_sql('PRAGMA journal_mode=WAL')
        event = self.once(); self.respond(event, 30, 'going')
        def authorized_then_change(*args):
            occurrence = _managed_event(*args)
            self.change(active=False)
            return occurrence
        with patch('lastz_bot.readiness._managed_event', side_effect=authorized_then_change):
            snapshot = self.roster(event)
        self.assertEqual((snapshot.eligible, snapshot.responded), (4, 1))
        self.assertEqual((self.roster(event).eligible, self.roster(event).responded), (3, 0))

    def test_roster_queries_are_read_only_and_keep_all_tables_unchanged(self):
        event = self.once('required'); self.respond(event, 30, 'going')
        def dump():
            with self.engine.connect() as connection:
                tables = connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").scalars().all()
                return {table: connection.exec_driver_sql(f'SELECT * FROM "{table}"').all() for table in tables}
        before = dump()
        for _ in range(2): readiness_page(self.roster(event), only_no_response=True)
        self.assertEqual(dump(), before)

    def test_large_roster_long_unicode_names_deterministic_pages(self):
        event = self.once('none')
        with self.sessions() as session:
            session.get(Event, event).name = '🚀'*1000
            session.get(Alliance, 1).name = 'Alliance ' + '🚀'*1000
            for i in range(150):
                session.add(Member(alliance_id=1, game_name=f'{i:03d} '+('🚀**@everyone\n'*100), rank='R1'))
            session.commit()
        roster = self.roster(event, actor=999, admin=True)
        first = readiness_page(roster)
        total = int(re.search(r'Page 1/(\d+)', first).group(1))
        self.assertGreater(total, 1)
        pages = [readiness_page(roster, page) for page in range(1, total+1)]
        self.assertEqual(sum(page.count('• ') for page in pages), 154)
        self.assertTrue(all(len(page.encode('utf-16-le'))//2 <= 2000 for page in pages))
        self.assertEqual(pages, [readiness_page(self.roster(event, actor=999, admin=True), page) for page in range(1, total+1)])
        for page in (0, total+1):
            with self.assertRaises(EventManagementError): readiness_page(roster, page)

    async def test_commands_ephemeral_no_mentions_single_response_and_no_response_filter(self):
        event = self.once('required'); self.respond(event, 30, 'going'); self.add_unlinked()
        for name in ('roster', 'no-response'):
            interaction = self.interaction()
            await self.commands[name](interaction, event)
            interaction.response.send_message.assert_awaited_once()
            call = interaction.response.send_message.call_args
            self.assertTrue(call.kwargs['ephemeral'])
            self.assertFalse(call.kwargs['allowed_mentions'].users)
            self.assertIn('Eligible: 5', call.args[0]); self.assertIn('Unlinked — unlinked', call.args[0])
            if name == 'no-response': self.assertNotIn('<@30>', call.args[0])

    async def test_page_requests_reauthorize_and_deleted_events_fail_safely(self):
        event = self.once()
        for i in range(100): self.add_unlinked(f'Member{i:03d}')
        for command in ('roster', 'no-response'):
            interaction = self.interaction()
            await self.commands[command](interaction, event, 2)
            self.assertIn('Page 2/', interaction.response.send_message.call_args.args[0])
            outsider = self.interaction(actor=30)
            await self.commands[command](outsider, event, 2)
            self.assertNotIn('Member', outsider.response.send_message.call_args.args[0])
        self.change(user=10, active=False)
        interaction = self.interaction()
        await self.commands['roster'](interaction, event, 2)
        self.assertNotIn('Page', interaction.response.send_message.call_args.args[0])
        delete_event(self.sessions, 1, event, 999, True)
        interaction = self.interaction(actor=999, admin=True)
        await self.commands['no-response'](interaction, event, 2)
        self.assertNotIn('Member', interaction.response.send_message.call_args.args[0])

    async def test_dm_command_denial_and_unknown_page(self):
        event = self.once()
        for command in ('roster', 'no-response'):
            interaction = self.interaction(guild=None)
            await self.commands[command](interaction, event)
            self.assertIn('Discord server', interaction.response.send_message.call_args.args[0])
            interaction = self.interaction()
            await self.commands[command](interaction, event, 999)
            self.assertIn('Choose a page', interaction.response.send_message.call_args.args[0])
