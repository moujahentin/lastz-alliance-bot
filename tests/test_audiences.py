"""Exact audiences, historical weekly snapshots, and authoritative RSVP/card checks."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event as Signal
import unittest
from unittest.mock import Mock

from sqlalchemy import event as sa_event, select, text
from sqlalchemy.exc import IntegrityError

from lastz_bot.audiences import audience_label, includes_rank, parse_audience
from lastz_bot.database.models import Alliance, Event, EventAudienceChange, EventRSVP, Member, WeeklySchedule
from lastz_bot.event_cards import EventCards
from lastz_bot.event_management import EventManagementError, edit_event
from lastz_bot.memberships import change_member
from lastz_bot.recurrence import WEEK, create_weekly, edit_series, ensure_occurrences
from lastz_bot.reminders import ReminderProcessor
from lastz_bot.rsvp import set_rsvp
import test_event_cards as cards


class AudienceTests(unittest.IsolatedAsyncioTestCase):
    connect = cards.EventCardTests.connect
    rows = cards.EventCardTests.rows
    interaction = cards.EventCardTests.interaction
    once = cards.EventCardTests.once
    series = cards.EventCardTests.series
    snapshot = cards.EventCardTests.snapshot
    add_channel = cards.EventCardTests.add_channel
    publish = cards.EventCardTests.publish
    press = cards.EventCardTests.press
    fields = cards.EventCardTests.fields

    def setUp(self):
        cards.EventCardTests.setUp(self)

    def change_rank(self, rank, user=30):
        change_member(self.sessions,1,'Alpha',20,False,discord_user_id=user,rank=rank)

    def edit_audience(self, occurrence, value):
        edit_event(self.sessions,1,occurrence,10,False,audience=value)

    def weekly(self, audience, start=None):
        with self.sessions() as session:
            result=create_weekly(session,session.get(Alliance,1),'Weekly','Text',
                                 start or self.start,10,self.now,participation='required',audience=audience)
            session.commit(); return result.id

    def test_parser_all_exact_rank_sets_and_invalid_inputs(self):
        for mask in range(1,32):
            value=audience_label(mask)
            self.assertEqual(parse_audience(value),mask)
            for i in range(5): self.assertEqual(includes_rank(mask,f'R{i+1}'),bool(mask & (1<<i)))
        self.assertEqual(parse_audience(' r4 + R1 + r4 '),9)
        for value in ('','MEMBER','R0','R6','R3,','Everyone,R3','R3 R4'):
            with self.subTest(value=value), self.assertRaises(EventManagementError): parse_audience(value)

    def test_everyone_optional_required_and_exact_audience_eligibility(self):
        for mode in ('optional','required'):
            occurrence=self.once(mode)
            for rank in ('R1','R2','R3','R4','R5'):
                self.change_rank(rank)
                set_rsvp(self.sessions,1,occurrence,30,'going')
            self.edit_audience(occurrence,'R1,R2,R4')
            for rank in ('R1','R2','R3','R4','R5'):
                self.change_rank(rank)
                if rank in ('R1','R2','R4'):
                    set_rsvp(self.sessions,1,occurrence,30,'maybe')
                else:
                    with self.assertRaisesRegex(EventManagementError,'outside'):
                        set_rsvp(self.sessions,1,occurrence,30,'not_going')
            with self.sessions() as session:
                self.assertEqual(session.get(EventRSVP,(occurrence,30)).response,'maybe')

    def test_current_rank_and_audience_changes_retain_old_intention(self):
        occurrence=self.once(); self.change_rank('R3'); self.edit_audience(occurrence,'R3')
        set_rsvp(self.sessions,1,occurrence,30,'going'); original=self.snapshot()
        self.change_rank('R4')
        with self.assertRaises(EventManagementError): set_rsvp(self.sessions,1,occurrence,30,'maybe')
        self.assertEqual(self.snapshot(),original)
        self.edit_audience(occurrence,'R4')
        self.assertEqual(self.snapshot(),original)
        set_rsvp(self.sessions,1,occurrence,30,'maybe')
        self.edit_audience(occurrence,'R1')
        with self.assertRaises(EventManagementError): set_rsvp(self.sessions,1,occurrence,30,'going')
        with self.sessions() as session: self.assertEqual(session.get(EventRSVP,(occurrence,30)).response,'maybe')

    def test_audience_none_independent_and_admin_has_no_eligibility_override(self):
        occurrence=self.once('none'); self.edit_audience(occurrence,'R1')
        with self.assertRaisesRegex(EventManagementError,'disabled'): set_rsvp(self.sessions,1,occurrence,30,'going')
        edit_event(self.sessions,1,occurrence,10,False,participation='required')
        for actor,guild in ((999,1),(40,1),(50,2)):
            with self.assertRaises(EventManagementError): set_rsvp(self.sessions,guild,occurrence,actor,'going')
        set_rsvp(self.sessions,1,occurrence,50,'going')  # Same user is R5 elsewhere, R1 here.
        self.edit_audience(occurrence,'R5')
        with self.assertRaises(EventManagementError): set_rsvp(self.sessions,1,occurrence,50,'maybe')

    def test_one_time_audit_noop_historical_guard_and_atomic_rejected_edit(self):
        occurrence=self.once(); self.edit_audience(occurrence,'R3'); self.edit_audience(occurrence,'R3')
        with self.sessions() as session:
            audit=session.scalars(select(EventAudienceChange)).all()
            self.assertEqual([(r.previous_audience,r.new_audience,r.actor_id) for r in audit],[(31,4,10)])
        with self.assertRaises(EventManagementError):
            edit_event(self.sessions,1,occurrence,10,False,audience='R4',starts_at='2026-09-20 17:00')
        with self.sessions() as session:
            self.assertEqual(session.get(Event,occurrence).audience,4)
            self.assertEqual(len(session.scalars(select(EventAudienceChange)).all()),1)
        self.now=self.start
        with self.assertRaisesRegex(EventManagementError,'Historical'): self.edit_audience(occurrence,'Everyone')

    def test_audience_management_respects_tenants_and_active_manager(self):
        occurrence=self.once()
        for guild,actor,admin in ((2,999,True),(1,40,False),(1,30,False),(1,999,False)):
            with self.assertRaises(EventManagementError): edit_event(self.sessions,guild,occurrence,actor,admin,audience='R3')
        change_member(self.sessions,1,'Alpha',20,False,discord_user_id=10,active=False)
        with self.assertRaises(EventManagementError): self.edit_audience(occurrence,'R3')
        edit_event(self.sessions,1,occurrence,999,True,audience='R3')

    def test_weekly_versions_preserve_past_and_future_identity_reminder_and_rsvp(self):
        series=self.weekly('R1,R3',self.start-WEEK); past,future=self.rows(series)
        set_rsvp(self.sessions,1,future.id,30,'going'); original=self.snapshot()
        processor=ReminderProcessor(self.sessions,Mock(),lambda:self.now); claim=processor.claim(future.id)
        edit_series(self.sessions,1,series,10,False,audience='R2,R4',now=self.now)
        rows=self.rows(series)
        self.assertEqual([(r.id,r.audience) for r in rows],[(past.id,5),(future.id,10)])
        self.assertEqual(self.snapshot(),original); self.assertIsNotNone(processor.current_delivery(claim))
        with self.sessions() as session:
            versions=session.scalars(select(WeeklySchedule).order_by(WeeklySchedule.id)).all()
            self.assertEqual([r.audience for r in versions],[5,10])
            self.assertEqual(versions[0].ends_at,self.now)
        self.now+=WEEK; ensure_occurrences(self.sessions,self.now)
        self.assertEqual(self.rows(series)[-1].audience,10)

    def test_weekly_historical_backfill_uses_old_audience_after_edit_and_restart(self):
        series=self.weekly('R3,R4,R5',self.start-120*WEEK)
        self.assertLessEqual(len(self.rows(series)),51)
        edit_series(self.sessions,1,series,10,False,audience='R2,R3,R4,R5',now=self.now)
        self.engine.dispose(); self.connect()
        for _ in range(4): ensure_occurrences(self.sessions,self.now)
        rows=self.rows(series)
        self.assertEqual(len(rows),121)
        self.assertTrue(all(row.audience==28 for row in rows if row.starts_at<=self.now))
        self.assertEqual(rows[-1].audience,30)
        ids=[row.id for row in rows]; ensure_occurrences(self.sessions,self.now)
        self.assertEqual([row.id for row in self.rows(series)],ids)

    def test_weekly_exceptions_cancellations_and_audience_overrides_preserved(self):
        series=self.weekly('R1'); occurrence=self.rows(series)[0]
        with self.sessions() as session:
            row=session.get(Event,occurrence.id); row.audience=4; row.audience_overridden=True
            row.is_exception=True; row.name='Exception'; session.commit()
        edit_series(self.sessions,1,series,10,False,audience='R4',now=self.now)
        row=self.rows(series)[0]
        self.assertEqual((row.id,row.audience,row.name),(occurrence.id,4,'Exception'))
        with self.sessions() as session:
            row=session.get(Event,occurrence.id); row.status='cancelled'; row.audience_overridden=False; session.commit()
        edit_series(self.sessions,1,series,10,False,audience='R5',now=self.now)
        self.assertEqual(self.rows(series)[0].audience,4)

    def test_weekly_time_change_retains_original_audience_on_cancelled_rows(self):
        series=self.weekly('R1'); occurrence=self.rows(series)[0]
        edit_series(self.sessions,1,series,10,False,audience='R4',time_at='18:00',now=self.now)
        old,new=self.rows(series)
        self.assertEqual((old.id,old.status,old.audience),(occurrence.id,'cancelled',1))
        self.assertEqual(new.audience,8)

    async def test_commands_create_edit_series_and_slash_rsvp_audience(self):
        for recurrence in ('once','weekly'):
            interaction=self.interaction()
            await self.commands['create'](interaction,'Alpha','Audience','2026-09-22 17:00',
                recurrence=recurrence,participation='optional',audience='R3 + R4')
            self.assertIn('created',interaction.response.send_message.call_args.args[0])
        once,weekly=self.rows()
        self.assertEqual((once.audience,weekly.audience),(12,12))
        interaction=self.interaction(actor=30)
        await self.commands['rsvp'](interaction,once.id,'going')
        self.assertIn('outside',interaction.response.send_message.call_args.args[0])
        interaction=self.interaction()
        await self.commands['edit'](interaction,once.id,audience='Everyone')
        interaction=self.interaction(actor=30)
        await self.commands['rsvp'](interaction,once.id,'going')
        self.assertIn('RSVP for occurrence',interaction.response.send_message.call_args.args[0])
        interaction=self.interaction()
        await self.commands['edit-series'](interaction,weekly.series_id,audience='R1,R2,R4')
        self.assertEqual(self.rows(weekly.series_id)[0].audience,11)

    async def test_cards_stale_controls_restart_and_multiple_cards(self):
        occurrence=self.once(); first=await self.publish(occurrence); second=await self.publish(occurrence)
        self.assertEqual(self.fields(first)['Audience'],'Everyone')
        self.edit_audience(occurrence,'R3,R4,R5')
        press=self.press(first); await self.cards.respond(press,'going')
        self.assertIn('outside',press.followup.send.call_args.args[0]); self.assertEqual(self.snapshot(),[])
        self.change_rank('R3')
        self.engine.dispose(); self.connect()
        restarted=EventCards(self.client,self.sessions); restarted.register()
        await restarted.respond(self.press(first),'going')
        for message in (first,second):
            self.assertEqual(self.fields(message)['Audience'],'R3, R4, R5')
            self.assertEqual(self.fields(message)['Going'],'1')
        change_member(self.sessions,1,'Alpha',20,False,discord_user_id=30,active=False)
        press=self.press(second); await restarted.respond(press,'maybe')
        self.assertIn('not a linked member',press.followup.send.call_args.args[0])
        self.assertEqual(len(self.snapshot()),1)

    def test_waiting_rsvp_cannot_use_rank_before_committed_change(self):
        occurrence=self.once(); self.edit_audience(occurrence,'R1'); attempted=Signal()
        def before(connection,cursor,statement,parameters,context,executemany):
            if statement=='BEGIN IMMEDIATE': attempted.set()
        with self.sessions() as writer, ThreadPoolExecutor(max_workers=1) as pool:
            writer.execute(text('BEGIN IMMEDIATE'))
            member=writer.scalar(select(Member).where(Member.alliance_id==1,Member.discord_user_id==30))
            member.rank='R2'
            sa_event.listen(self.engine,'before_cursor_execute',before)
            future=pool.submit(set_rsvp,self.sessions,1,occurrence,30,'going')
            try:
                self.assertTrue(attempted.wait(3)); writer.commit()
                with self.assertRaisesRegex(EventManagementError,'outside'): future.result(timeout=5)
            finally:
                writer.rollback(); sa_event.remove(self.engine,'before_cursor_execute',before)
        self.assertEqual(self.snapshot(),[])

    def test_database_rejects_empty_or_invalid_audience(self):
        occurrence=self.once()
        for value in (0,32,-1):
            with self.subTest(value=value),self.assertRaises(IntegrityError),self.sessions() as session:
                session.get(Event,occurrence).audience=value; session.commit()


    async def test_invalid_create_audience_writes_nothing_and_list_labels_restricted_set(self):
        for recurrence in ('once', 'weekly'):
            interaction = self.interaction()
            await self.commands['create'](interaction, 'Alpha', 'Invalid', '2026-09-22 17:00',
                                          recurrence=recurrence, audience='R3,R9')
            self.assertIn('Audience must', interaction.response.send_message.call_args.args[0])
        self.assertEqual(self.rows(), [])
        occurrence = self.once()
        self.edit_audience(occurrence, 'R1,R2,R4')
        interaction = self.interaction()
        await self.commands['list'](interaction, 'Alpha')
        self.assertIn('Audience: R1, R2, R4', interaction.response.send_message.call_args.args[0])

    def test_weekly_audience_noop_creates_no_version_and_rechecks_clock_after_lock(self):
        series = self.weekly('R3,R4,R5')
        occurrence = self.rows(series)[0]
        edit_series(self.sessions, 1, series, 10, False, audience='R5+R4+R3', now=self.now)
        with self.sessions() as session:
            self.assertEqual(len(session.scalars(select(WeeklySchedule)).all()), 1)
        def advance(connection, cursor, statement, parameters, context, executemany):
            if statement == 'BEGIN IMMEDIATE': self.now = self.start
        sa_event.listen(self.engine, 'after_cursor_execute', advance)
        try:
            edit_series(self.sessions, 1, series, 10, False, audience='R1')
        finally:
            sa_event.remove(self.engine, 'after_cursor_execute', advance)
        with self.sessions() as session:
            self.assertEqual(session.get(Event, occurrence.id).audience, 28)
        self.assertEqual(self.rows(series)[-1].audience, 1)

    def test_waiting_rsvp_observes_new_audience_and_retains_committed_response(self):
        occurrence = self.once()
        set_rsvp(self.sessions, 1, occurrence, 30, 'going')
        original = self.snapshot()
        attempted = Signal()
        def before(connection, cursor, statement, parameters, context, executemany):
            if statement == 'BEGIN IMMEDIATE': attempted.set()
        with self.sessions() as writer, ThreadPoolExecutor(max_workers=1) as pool:
            writer.execute(text('BEGIN IMMEDIATE'))
            writer.get(Event, occurrence).audience = 4
            sa_event.listen(self.engine, 'before_cursor_execute', before)
            future = pool.submit(set_rsvp, self.sessions, 1, occurrence, 30, 'maybe')
            try:
                self.assertTrue(attempted.wait(3)); writer.commit()
                with self.assertRaisesRegex(EventManagementError, 'outside'): future.result(timeout=5)
            finally:
                writer.rollback(); sa_event.remove(self.engine, 'before_cursor_execute', before)
        self.assertEqual(self.snapshot(), original)
