"""Reporting deadlines, current eligibility, and terminal targeted DM delivery."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event as Signal
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from sqlalchemy import event as sa_event, select, text
from sqlalchemy.exc import IntegrityError

from lastz_bot.database.models import Alliance, Event, EventRSVP, EventSeries, Member, RSVPReminder, WeeklySchedule
from lastz_bot.event_management import EventManagementError, delete_event, edit_event
from lastz_bot.event_time import utc_to_apocalypse_time
from lastz_bot.memberships import change_member
from lastz_bot.recurrence import WEEK, create_weekly, edit_series, ensure_occurrences, stop_series
from lastz_bot.reminder_worker import ReminderWorker
from lastz_bot.rsvp import get_rsvps, set_rsvp, summary_pages
from lastz_bot.rsvp_policy import validate_deadline, validate_relative
from lastz_bot.rsvp_reminders import RSVPReminderProcessor
import test_event_cards as cards


class RSVPDeadlineTests(unittest.IsolatedAsyncioTestCase):
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
        self.dm = AsyncMock()
        async def transport(delivery):
            current = self.processor.authorize_delivery(delivery)
            if current is None:
                raise RuntimeError('No longer authorized')
            await self.dm(current)
        self.processor = RSVPReminderProcessor(self.sessions, transport, lambda: self.now)

    def at(self, value):
        return utc_to_apocalypse_time(value).strftime('%Y-%m-%d %H:%M')

    def configure(self, occurrence, deadline=None, enabled=True):
        edit_event(self.sessions, 1, occurrence, 10, False,
                   rsvp_deadline=self.at(deadline or self.start-timedelta(minutes=10)), missing_reminder=enabled)

    def member_id(self, user=30, alliance=1):
        with self.sessions() as session:
            return session.scalar(select(Member.id).where(Member.alliance_id==alliance, Member.discord_user_id==user))

    def summary(self, occurrence):
        return get_rsvps(self.sessions,1,occurrence,10,False)

    def attempts(self):
        with self.sessions() as session:
            return session.scalars(select(RSVPReminder).order_by(RSVPReminder.discord_user_id)).all()

    def weekly(self, minutes=10, enabled=True, start=None):
        with self.sessions() as session:
            row=create_weekly(session,session.get(Alliance,1),'Weekly','Details',start or self.start,
                              10,self.now,participation='required',deadline_minutes=minutes,missing_reminder=enabled)
            session.commit(); return row.id

    def change(self, user=30, **values):
        change_member(self.sessions,1,'Alpha',999,True,discord_user_id=user,**values)

    async def test_create_at_deadline_storage_and_default_disabled(self):
        interaction=self.interaction()
        await self.commands['create'](interaction,'Alpha','Deadline','2026-09-22 17:00',
            participation='required',rsvp_deadline='2026-09-22 16:45')
        row=self.rows()[0]
        self.assertEqual(row.rsvp_deadline,self.start-timedelta(minutes=15))
        self.assertIsNone(row.rsvp_deadline.tzinfo); self.assertFalse(row.missing_reminder)
        await self.processor.process_pending(); self.dm.assert_not_awaited()
        message=await self.publish(row.id)
        self.assertEqual(self.fields(message)['RSVP Deadline'],'2026-09-22 16:45 AT')

    async def test_invalid_deadline_create_rejects_equal_after_and_enabled_without_deadline(self):
        for options in ({'rsvp_deadline':'2026-09-22 17:00'}, {'rsvp_deadline':'2026-09-22 17:01'},
                        {'missing_reminder':True}, {'rsvp_deadline':'invalid'}, {'deadline_minutes':10}):
            interaction=self.interaction()
            await self.commands['create'](interaction,'Alpha','Bad','2026-09-22 17:00',**options)
            self.assertTrue(interaction.followup.send.call_args.args[0].startswith('❌'))
        self.assertEqual(self.rows(),[])

    def test_business_and_database_deadline_validation(self):
        occurrence=self.once()
        for deadline in (self.start,self.start+timedelta(minutes=1)):
            with self.assertRaises(EventManagementError): validate_deadline(self.start,deadline,False)
            with self.assertRaises(IntegrityError),self.sessions() as session:
                session.get(Event,occurrence).rsvp_deadline=deadline; session.commit()
        with self.assertRaises(EventManagementError): validate_deadline(self.start,None,True)
        with self.assertRaises(IntegrityError),self.sessions() as session:
            session.get(Event,occurrence).missing_reminder=True; session.commit()

    async def test_past_deadline_future_start_allows_late_response_changes_and_no_dm(self):
        interaction=self.interaction()
        await self.commands['create'](interaction,'Alpha','Late','2026-09-22 17:00',
            participation='required',rsvp_deadline='2026-09-21 22:00',missing_reminder=True)
        occurrence=self.rows()[0].id
        for response in ('going','maybe','not_going'): set_rsvp(self.sessions,1,occurrence,30,response)
        await self.processor.process_pending(); self.dm.assert_not_awaited()
        self.assertEqual(self.summary(occurrence).groups['not_going'],(30,))
        message=await self.publish(occurrence)
        self.assertIn('RSVP deadline passed',self.fields(message)); self.assertIsNotNone(message.view)
        await self.cards.respond(self.press(message),'going')
        self.assertEqual(self.summary(occurrence).groups['going'],(30,))

    def test_edit_clear_preserves_rsvp_and_requires_explicit_disable(self):
        occurrence=self.once('required'); self.configure(occurrence)
        set_rsvp(self.sessions,1,occurrence,30,'going'); snapshot=self.snapshot()
        with self.assertRaisesRegex(EventManagementError,'Disable'):
            edit_event(self.sessions,1,occurrence,10,False,rsvp_deadline='none')
        self.assertIsNotNone(self.rows()[0].rsvp_deadline)
        edit_event(self.sessions,1,occurrence,10,False,rsvp_deadline='none',missing_reminder=False)
        self.assertIsNone(self.rows()[0].rsvp_deadline); self.assertEqual(self.snapshot(),snapshot)

    def test_reschedule_earlier_rejects_invalid_existing_deadline_later_keeps_it(self):
        occurrence=self.once('required'); self.configure(occurrence)
        set_rsvp(self.sessions,1,occurrence,30,'going'); snapshot=self.snapshot()
        with self.assertRaisesRegex(EventManagementError,'strictly before'):
            edit_event(self.sessions,1,occurrence,10,False,starts_at=self.at(self.start-timedelta(minutes=15)))
        self.assertEqual(self.rows()[0].starts_at,self.start)
        deadline=self.rows()[0].rsvp_deadline
        edit_event(self.sessions,1,occurrence,10,False,starts_at=self.at(self.start+timedelta(hours=1)))
        self.assertEqual(self.rows()[0].rsvp_deadline,deadline); self.assertEqual(self.snapshot(),snapshot)
        edit_event(self.sessions,1,occurrence,10,False,starts_at=self.at(self.start-timedelta(minutes=15)),
                   rsvp_deadline=self.at(self.start-timedelta(minutes=20)))
        self.assertLess(self.rows()[0].rsvp_deadline,self.rows()[0].starts_at)

    def test_no_response_excludes_all_three_responses_and_includes_unlinked(self):
        occurrence=self.once('required')
        for user,response in ((10,'going'),(20,'maybe'),(30,'not_going')):
            set_rsvp(self.sessions,1,occurrence,user,response)
        with self.sessions() as session:
            session.add(Member(alliance_id=1,game_name='Unlinked',rank='R3')); session.commit()
        summary=self.summary(occurrence)
        self.assertEqual({row.discord_user_id for row in summary.no_response},{50,None})
        self.assertEqual(len(self.snapshot()),3)
        display='\n'.join(summary_pages(summary))
        for label in ('Going (1)','Maybe (1)','Not Going (1)','No Response (2)','Unlinked','cannot DM'):
            self.assertIn(label,display)

    def test_current_rank_activity_audience_changes_and_tenant_isolation(self):
        occurrence=self.once('required')
        edit_event(self.sessions,1,occurrence,10,False,audience='R1')
        self.assertEqual({row.discord_user_id for row in self.summary(occurrence).no_response},{30,50})
        self.change(rank='R4')
        self.assertEqual({row.discord_user_id for row in self.summary(occurrence).no_response},{50})
        edit_event(self.sessions,1,occurrence,10,False,audience='R1,R4')
        self.assertEqual({row.discord_user_id for row in self.summary(occurrence).no_response},{10,30,50})
        self.change(active=False)
        self.assertEqual({row.discord_user_id for row in self.summary(occurrence).no_response},{10,50})
        for guild,actor,admin in ((1,40,False),(2,50,True),(1,30,False)):
            with self.assertRaises(EventManagementError): get_rsvps(self.sessions,guild,occurrence,actor,admin)

    async def test_optional_none_never_mandatory_and_participation_edits_recalculate(self):
        occurrence=self.once('required'); self.configure(occurrence)
        set_rsvp(self.sessions,1,occurrence,30,'maybe'); snapshot=self.snapshot()
        message=await self.publish(occurrence)
        self.assertEqual(self.fields(message)['No Response'],'3')
        for mode in ('optional','none'):
            edit_event(self.sessions,1,occurrence,10,False,participation=mode)
            self.assertEqual(self.summary(occurrence).no_response,())
            self.assertNotIn('No Response','\n'.join(summary_pages(self.summary(occurrence))))
            await self.cards.refresh(); self.assertNotIn('No Response',self.fields(message))
            await self.processor.process_pending(); self.dm.assert_not_awaited()
        edit_event(self.sessions,1,occurrence,10,False,participation='required')
        self.assertEqual(len(self.summary(occurrence).no_response),3); self.assertEqual(self.snapshot(),snapshot)

    async def test_card_reconciliation_current_eligibility_deadline_and_multiple_cards(self):
        occurrence=self.once('required'); first=await self.publish(occurrence); second=await self.publish(occurrence)
        self.configure(occurrence); self.change(active=False)
        await self.cards.refresh()
        for message in (first,second):
            self.assertEqual(self.fields(message)['No Response'],'3')
            self.assertIn('RSVP Deadline',self.fields(message))
        self.now=self.start-timedelta(minutes=10)
        await self.cards.refresh()
        for message in (first,second):
            self.assertIn('RSVP deadline passed',self.fields(message)); self.assertIsNotNone(message.view)
        await self.cards.respond(self.press(first),'going')
        self.assertEqual(self.snapshot(),[])

    async def test_opt_in_threshold_exact_boundary_deadline_and_no_duplicate(self):
        occurrence=self.once('required')
        await self.processor.process_pending(); self.dm.assert_not_awaited()
        self.configure(occurrence)
        deadline=self.rows()[0].rsvp_deadline
        self.now=deadline-timedelta(minutes=60,seconds=1)
        await self.processor.process_pending(); self.dm.assert_not_awaited()
        self.now+=timedelta(seconds=1)
        await self.processor.process_pending(); self.assertEqual(self.dm.await_count,4)
        await self.processor.process_pending(); self.assertEqual(self.dm.await_count,4)
        self.assertTrue(all(row.status=='sent' for row in self.attempts()))
        self.now=deadline
        self.change(rank='R3')
        await self.processor.process_pending(); self.assertEqual(self.dm.await_count,4)

    async def test_only_current_linked_nonresponders_are_claimed(self):
        occurrence=self.once('required'); self.configure(occurrence)
        edit_event(self.sessions,1,occurrence,10,False,audience='R1')
        set_rsvp(self.sessions,1,occurrence,30,'not_going')
        with self.sessions() as session:
            session.add(Member(alliance_id=1,game_name='Unlinked',rank='R1')); session.commit()
        await self.processor.process_pending()
        self.assertEqual(self.dm.await_count,1)
        self.assertEqual(self.dm.call_args.args[0].discord_user_id,50)
        self.assertEqual(len(self.summary(occurrence).no_response),2)
        self.assertEqual(len(self.attempts()),1)
        self.assertIsNone(self.processor.claim(occurrence,self.member_id(40,2)))

    async def test_restart_overlapping_workers_and_uncertain_failure_do_not_retry(self):
        occurrence=self.once('required'); self.configure(occurrence)
        self.dm.side_effect=RuntimeError('DM denied or uncertain')
        with self.assertLogs('lastz_bot.rsvp_reminders',level='WARNING'):
            await self.processor.process_pending()
        self.assertTrue(all(row.status=='attempted' and row.sent_at is None for row in self.attempts()))
        self.engine.dispose(); self.connect()
        restarted=RSVPReminderProcessor(self.sessions,AsyncMock(),lambda:self.now)
        await restarted.process_pending(); restarted.send.assert_not_awaited()
        self.assertEqual(len(self.attempts()),4)

    def test_two_process_claims_have_one_winner(self):
        occurrence=self.once('required'); self.configure(occurrence); member=self.member_id()
        other=RSVPReminderProcessor(self.sessions,AsyncMock(),lambda:self.now)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda p:p.claim(occurrence,member),(self.processor,other)))
        self.assertEqual(sum(result is not None for result in results),1)
        self.assertEqual(len(self.attempts()),1)

    def test_final_authorization_rechecks_responses_rank_state_and_window(self):
        occurrence=self.once('required'); self.configure(occurrence)
        delivery=self.processor.claim(occurrence,self.member_id())
        for response in ('going','maybe','not_going'):
            set_rsvp(self.sessions,1,occurrence,30,response)
            self.assertIsNone(self.processor.current_delivery(delivery))
        delivery2=self.processor.claim(occurrence,self.member_id(50))
        self.change(user=50,active=False)
        self.assertIsNone(self.processor.current_delivery(delivery2))
        self.change(user=50,active=True)
        edit_event(self.sessions,1,occurrence,10,False,audience='R4')
        self.assertIsNone(self.processor.current_delivery(delivery2))
        edit_event(self.sessions,1,occurrence,10,False,audience='Everyone')
        self.now=delivery2.deadline
        self.assertIsNone(self.processor.current_delivery(delivery2))

    def test_claim_policy_edit_away_back_reschedule_and_stale_completion(self):
        occurrence=self.once('required'); self.configure(occurrence)
        delivery=self.processor.claim(occurrence,self.member_id())
        original=self.rows()[0].rsvp_deadline
        self.configure(occurrence,original-timedelta(minutes=1))
        self.configure(occurrence,original)
        self.assertIsNone(self.processor.current_delivery(delivery))
        self.processor.mark_sent(delivery)
        self.assertEqual(self.attempts()[0].status,'claimed')
        self.assertIsNone(self.processor.claim(occurrence,self.member_id()))
        delivery2=self.processor.claim(occurrence,self.member_id(50))
        edit_event(self.sessions,1,occurrence,10,False,starts_at=self.at(self.start+timedelta(hours=1)))
        self.assertIsNone(self.processor.current_delivery(delivery2))

    def test_event_delete_id_reuse_never_reauthorizes_old_claim(self):
        occurrence=self.once('required'); self.configure(occurrence)
        old=self.processor.claim(occurrence,self.member_id())
        delete_event(self.sessions,1,occurrence,10,False)
        replacement=self.once('required'); self.assertEqual(replacement,occurrence)
        self.configure(replacement)
        new=self.processor.claim(replacement,self.member_id())
        self.assertNotEqual(old.claim_token,new.claim_token)
        self.assertIsNone(self.processor.current_delivery(old))
        self.processor.mark_sent(old); self.assertEqual(self.attempts()[0].status,'claimed')

    def test_final_authorization_waits_for_committed_rsvp(self):
        occurrence=self.once('required'); self.configure(occurrence)
        delivery=self.processor.claim(occurrence,self.member_id()); attempted=Signal()
        def before(connection,cursor,statement,parameters,context,executemany):
            if statement=='BEGIN IMMEDIATE': attempted.set()
        with self.sessions() as writer,ThreadPoolExecutor(max_workers=1) as pool:
            writer.execute(text('BEGIN IMMEDIATE'))
            writer.add(EventRSVP(event_id=occurrence,discord_user_id=30,response='going',created_at=self.now,updated_at=self.now))
            sa_event.listen(self.engine,'before_cursor_execute',before)
            future=pool.submit(self.processor.current_delivery,delivery)
            try:
                self.assertTrue(attempted.wait(3)); writer.commit(); self.assertIsNone(future.result(timeout=5))
            finally:
                writer.rollback(); sa_event.remove(self.engine,'before_cursor_execute',before)

    async def test_transport_lookup_rsvp_race_suppresses_send(self):
        occurrence=self.once('required'); self.configure(occurrence)
        delivery=self.processor.claim(occurrence,self.member_id())
        client=Mock(); client.get_user.return_value=None
        user=Mock(); user.id=30; channel=Mock(); channel.send=AsyncMock()
        async def lookup(user_id):
            set_rsvp(self.sessions,1,occurrence,30,'going')
            return user
        client.fetch_user=AsyncMock(side_effect=lookup)
        user.create_dm=AsyncMock(return_value=channel)
        worker=ReminderWorker(client); worker.rsvp_processor=self.processor
        with self.assertRaises(RuntimeError): await worker.send_rsvp(delivery)
        channel.send.assert_not_awaited()

    async def test_transport_useful_private_message_and_in_flight_edit_no_retry(self):
        occurrence=self.once('required'); self.configure(occurrence)
        delivery=self.processor.claim(occurrence,self.member_id())
        client=Mock(); user=client.get_user.return_value; user.id=30
        channel=Mock(); channel.send=AsyncMock(); user.create_dm=AsyncMock(return_value=channel)
        worker=ReminderWorker(client); worker.rsvp_processor=self.processor
        await worker.send_rsvp(delivery)
        message=channel.send.call_args.args[0]
        for expected in ('Duel','Alpha','2026-09-22 17:00','2026-09-22 16:50','still missing','event card'):
            self.assertIn(expected,message)
        self.assertEqual(channel.send.call_args.kwargs['allowed_mentions'].to_dict(),{'parse':[]})
        self.assertNotIn('<@',message)
        async def edit_in_flight(*args,**kwargs):
            edit_event(self.sessions,1,occurrence,10,False,missing_reminder=False)
        self.processor.mark_sent(delivery)
        with self.assertRaises(RuntimeError): await worker.send_rsvp(delivery)
        delivery=self.processor.claim(occurrence,self.member_id(50))
        user.id=50
        channel.send.side_effect=edit_in_flight
        await worker.send_rsvp(delivery)
        self.processor.mark_sent(delivery)
        self.assertEqual(self.attempts()[-1].status,'attempted')
        edit_event(self.sessions,1,occurrence,10,False,missing_reminder=True)
        self.assertIsNone(self.processor.claim(occurrence,self.member_id()))

    def test_weekly_relative_deadlines_versioning_and_past_backfill(self):
        series=self.weekly(minutes=120,start=self.start-120*WEEK)
        self.assertEqual(len(self.rows(series)),51)
        edit_series(self.sessions,1,series,10,False,deadline_minutes=180,now=self.now)
        for _ in range(4): ensure_occurrences(self.sessions,self.now)
        rows=self.rows(series); self.assertEqual(len(rows),121)
        for row in rows:
            offset=120 if row.starts_at<=self.now else 180
            self.assertEqual(row.rsvp_deadline,row.starts_at-timedelta(minutes=offset))
            self.assertTrue(row.missing_reminder)
        with self.sessions() as session:
            self.assertEqual([s.deadline_minutes for s in session.scalars(select(WeeklySchedule).order_by(WeeklySchedule.id))],[120,180])
        before=[row.id for row in rows]; ensure_occurrences(self.sessions,self.now)
        self.assertEqual([row.id for row in self.rows(series)],before)

    def test_weekly_future_ids_rsvps_and_overrides_survive_policy_edit(self):
        series=self.weekly(); occurrence=self.rows(series)[0]
        set_rsvp(self.sessions,1,occurrence.id,30,'going'); snapshot=self.snapshot()
        claim=self.processor.claim(occurrence.id,self.member_id(50))
        edit_series(self.sessions,1,series,10,False,deadline_minutes=20,now=self.now)
        row=self.rows(series)[0]
        self.assertEqual(row.id,occurrence.id); self.assertEqual(row.rsvp_deadline,self.start-timedelta(minutes=20))
        self.assertEqual(self.snapshot(),snapshot); self.assertIsNone(self.processor.current_delivery(claim))
        with self.sessions() as session:
            row=session.get(Event,occurrence.id); row.deadline_overridden=True; session.commit()
        edit_series(self.sessions,1,series,10,False,deadline_minutes=15,now=self.now)
        self.assertEqual(self.rows(series)[0].rsvp_deadline,self.start-timedelta(minutes=20))

    async def test_weekly_no_expired_reminders_and_stop_revokes_claims(self):
        series=self.weekly(start=self.start-WEEK); past,future=self.rows(series)
        self.assertIsNone(self.processor.claim(past.id,self.member_id()))
        delivery=self.processor.claim(future.id,self.member_id())
        stop_series(self.sessions,1,series,10,False,now=self.now)
        self.assertIsNone(self.processor.current_delivery(delivery))
        await self.processor.process_pending(); self.dm.assert_not_awaited()
        self.assertEqual(self.rows(series)[0].rsvp_deadline,past.rsvp_deadline)

    def test_weekly_clear_and_invalid_offsets_are_atomic(self):
        series=self.weekly()
        with self.assertRaises(EventManagementError): edit_series(self.sessions,1,series,10,False,deadline_minutes=0,now=self.now)
        for invalid in (-1,1.5):
            with self.assertRaises(EventManagementError): validate_relative(invalid,False)
        edit_series(self.sessions,1,series,10,False,deadline_minutes=0,missing_reminder=False,now=self.now)
        row=self.rows(series)[0]; self.assertIsNone(row.rsvp_deadline); self.assertFalse(row.missing_reminder)

    async def test_weekly_command_relative_options_and_one_time_clear(self):
        interaction=self.interaction()
        await self.commands['create'](interaction,'Alpha','Weekly','2026-09-22 17:00',recurrence='weekly',
            participation='required',deadline_minutes=120,missing_reminder=True)
        row=self.rows()[0]; self.assertEqual(row.rsvp_deadline,self.start-timedelta(hours=2))
        interaction=self.interaction()
        await self.commands['edit-series'](interaction,row.series_id,deadline_minutes=0,missing_reminder=False)
        self.assertIsNone(self.rows()[0].rsvp_deadline)
        occurrence=self.once('required'); self.configure(occurrence)
        interaction=self.interaction()
        await self.commands['edit'](interaction,occurrence,rsvp_deadline='none',missing_reminder=False)
        with self.sessions() as session: self.assertIsNone(session.get(Event,occurrence).rsvp_deadline)


    def test_policy_and_membership_roundtrips_cannot_resurrect_claims(self):
        for kind in ('deadline','enabled','optional','none','audience','rank','active','link','start'):
            with self.subTest(kind=kind):
                occurrence=self.once('required'); self.configure(occurrence)
                delivery=self.processor.claim(occurrence,self.member_id())
                self.assertIsNotNone(delivery)
                if kind=='deadline':
                    self.configure(occurrence,delivery.deadline-timedelta(minutes=1)); self.configure(occurrence,delivery.deadline)
                elif kind=='enabled':
                    edit_event(self.sessions,1,occurrence,10,False,missing_reminder=False)
                    edit_event(self.sessions,1,occurrence,10,False,missing_reminder=True)
                elif kind in ('optional','none'):
                    edit_event(self.sessions,1,occurrence,10,False,participation=kind)
                    edit_event(self.sessions,1,occurrence,10,False,participation='required')
                elif kind=='audience':
                    edit_event(self.sessions,1,occurrence,10,False,audience='R4')
                    edit_event(self.sessions,1,occurrence,10,False,audience='Everyone')
                elif kind=='rank':
                    self.change(rank='R2'); self.change(rank='R1')
                elif kind=='active':
                    self.change(active=False); self.change(active=True)
                elif kind=='link':
                    self.change(link_to=31); self.change(user=31,link_to=30)
                else:
                    edit_event(self.sessions,1,occurrence,10,False,starts_at=self.at(self.start+timedelta(hours=1)))
                    edit_event(self.sessions,1,occurrence,10,False,starts_at=self.at(self.start))
                self.assertIsNone(self.processor.authorize_delivery(delivery))
                self.assertIsNone(self.processor.claim(occurrence,self.member_id()))

    def test_weekly_audience_policy_roundtrips_cannot_resurrect_claims(self):
        for kind in ('audience','participation','deadline_minutes','missing_reminder'):
            series=self.weekly(); occurrence=self.rows(series)[0]
            delivery=self.processor.claim(occurrence.id,self.member_id())
            away={'audience':'R4','participation':'optional','deadline_minutes':15,'missing_reminder':False}[kind]
            back={'audience':'Everyone','participation':'required','deadline_minutes':10,'missing_reminder':True}[kind]
            edit_series(self.sessions,1,series,10,False,now=self.now,**{kind:away})
            edit_series(self.sessions,1,series,10,False,now=self.now,**{kind:back})
            self.assertIsNone(self.processor.authorize_delivery(delivery))
            self.assertIsNone(self.processor.claim(occurrence.id,self.member_id()))

    def test_final_authorization_is_one_time_under_concurrent_replay(self):
        occurrence=self.once('required'); self.configure(occurrence)
        delivery=self.processor.claim(occurrence,self.member_id())
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(self.processor.authorize_delivery,(delivery,delivery)))
        self.assertEqual(sum(result is not None for result in results),1)
        self.assertEqual(self.attempts()[0].status,'attempted')
        self.processor.mark_sent(delivery)
        self.assertEqual(self.attempts()[0].status,'sent')
        self.assertIsNone(self.processor.authorize_delivery(delivery))

    async def test_real_transport_dm_forbidden_keeps_attempt_terminal_without_success(self):
        import discord
        occurrence=self.once('required'); self.configure(occurrence)
        edit_event(self.sessions,1,occurrence,10,False,audience='R5')
        client=Mock(); user=client.get_user.return_value; user.id=20
        channel=Mock()
        channel.send=AsyncMock(side_effect=discord.Forbidden(SimpleNamespace(status=403,reason='Forbidden'), 'Cannot DM'))
        user.create_dm=AsyncMock(return_value=channel)
        worker=ReminderWorker(client); worker.rsvp_processor=self.processor
        self.processor.send=worker.send_rsvp
        with self.assertLogs('lastz_bot.rsvp_reminders',level='WARNING'):
            await self.processor.process_pending()
        self.assertEqual([(r.status,r.sent_at) for r in self.attempts()],[('attempted',None)])
        await self.processor.process_pending(); channel.send.assert_awaited_once()

    async def test_dm_channel_resolution_race_and_lookup_failure_are_not_success(self):
        occurrence=self.once('required'); self.configure(occurrence)
        delivery=self.processor.claim(occurrence,self.member_id())
        client=Mock(); user=client.get_user.return_value; user.id=30
        channel=Mock(); channel.send=AsyncMock()
        async def create_dm():
            self.change(active=False)
            return channel
        user.create_dm=AsyncMock(side_effect=create_dm)
        worker=ReminderWorker(client); worker.rsvp_processor=self.processor
        with self.assertRaises(RuntimeError): await worker.send_rsvp(delivery)
        channel.send.assert_not_awaited()
        self.assertIsNone(self.attempts()[0].sent_at)
        self.change(active=True)
        self.assertIsNone(self.processor.authorize_delivery(delivery))
        user.create_dm.side_effect=RuntimeError('Lookup failed')
        other=self.processor.claim(occurrence,self.member_id(50)); user.id=50
        with self.assertRaises(RuntimeError): await worker.send_rsvp(other)
        self.assertEqual(self.attempts()[-1].status,'claimed')

    def test_noop_changes_preserve_pending_authority(self):
        occurrence=self.once('required'); self.configure(occurrence)
        delivery=self.processor.claim(occurrence,self.member_id())
        self.configure(occurrence,delivery.deadline)
        self.change(rank='R1',active=True)
        edit_event(self.sessions,1,occurrence,10,False,audience='Everyone',participation='required',name='Renamed')
        current=self.processor.authorize_delivery(delivery)
        self.assertIsNotNone(current); self.assertEqual(current.event_name,'Renamed')

    async def test_overlapping_cycles_send_once_each(self):
        import asyncio
        occurrence=self.once('required'); self.configure(occurrence)
        await asyncio.gather(self.processor.process_pending(),self.processor.process_pending())
        self.assertEqual(self.dm.await_count,4)
        self.assertEqual(len(self.attempts()),4)
