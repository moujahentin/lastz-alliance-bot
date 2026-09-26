"""Player delivery integration: persistent attempts, exact audiences and races."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from lastz_bot.automatic_publications import authorize_automatic, register_automatic, reserve_automatic
from lastz_bot.commands.alliance import setup_alliance_commands
from lastz_bot.commands.event import setup_event_commands
from lastz_bot.commands.member import setup_member_commands
from lastz_bot.database.models import Alliance, AutomaticPublication, Event, EventPublication, Member, PlayerReminder
from lastz_bot.event_cards import EventCards
from lastz_bot.event_management import EventManagementError, delete_event, edit_event
from lastz_bot.event_time import discord_timestamp, utc_to_apocalypse_time
from lastz_bot.memberships import change_member
from lastz_bot.player_events import card_navigation, discovery_text, personal_events
from lastz_bot.player_policy import configure_delivery
from lastz_bot.player_reminders import PlayerDelivery, PlayerReminderProcessor, due_lead
from lastz_bot.publications import abandon_publication, prune_pending_publications
from lastz_bot.recurrence import ensure_occurrences, stop_series
from lastz_bot.reminder_worker import ReminderWorker
import test_event_cards as cards


CLAIM_PROCESS = '''
import json, sys
from datetime import datetime
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from lastz_bot.automatic_publications import reserve_automatic, authorize_automatic
from lastz_bot.player_reminders import PlayerDelivery, PlayerReminderProcessor
url, mode, raw = sys.argv[1:]
data = json.loads(raw)
engine = create_engine(url)
@event.listens_for(engine, "connect")
def foreign_keys(connection, record):
    connection.execute("PRAGMA foreign_keys=ON")
sessions = sessionmaker(bind=engine, autoflush=False)
clock = lambda: datetime.fromisoformat(data["now"])
print("ready", flush=True)
sys.stdin.readline()
if mode == "auto_claim":
    result = reserve_automatic(sessions, data["event"], 1, 101, clock)
elif mode == "auto_authorize":
    result = authorize_automatic(sessions, data["reservation"], clock)
else:
    processor = PlayerReminderProcessor(sessions, None, clock)
    if mode == "player_claim":
        result = processor.claim(data["event"], data["member"])
    else:
        delivery = data["delivery"]
        delivery["starts_at"] = datetime.fromisoformat(delivery["starts_at"])
        result = processor.authorize_delivery(PlayerDelivery(**delivery))
print(int(result is not None), flush=True)
engine.dispose()
'''


class PlayerExperienceTests(unittest.IsolatedAsyncioTestCase):
    connect = cards.EventCardTests.connect
    rows = cards.EventCardTests.rows
    interaction = cards.EventCardTests.interaction
    once = cards.EventCardTests.once
    series = cards.EventCardTests.series
    snapshot = cards.EventCardTests.snapshot
    add_channel = cards.EventCardTests.add_channel
    publish = cards.EventCardTests.publish
    fields = cards.EventCardTests.fields
    publications = cards.EventCardTests.publications

    def setUp(self):
        cards.EventCardTests.setUp(self)
        clock = patch('lastz_bot.event_cards.utc_now_naive', side_effect=lambda: self.now)
        clock.start(); self.addCleanup(clock.stop)
        self.dm = AsyncMock()
        async def send(delivery):
            current = self.processor.authorize_delivery(delivery)
            if current is None:
                raise RuntimeError('Revoked')
            await self.dm(current)
        self.processor = PlayerReminderProcessor(self.sessions, send, lambda: self.now)

    def configure(self, **kwargs):
        return configure_delivery(self.sessions, 1, 'Alpha', 10, False, **kwargs)

    def member(self, user=30, alliance=1):
        with self.sessions() as session:
            return session.scalar(select(Member.id).where(Member.alliance_id == alliance, Member.discord_user_id == user))

    def change(self, **kwargs):
        change_member(self.sessions, 1, 'Alpha', 999, True, discord_user_id=30, **kwargs)

    def edit_event(self, event, **kwargs):
        edit_event(self.sessions, 1, event, 10, False, **kwargs)

    def reserve(self, event):
        return reserve_automatic(self.sessions, event, 1, 101, lambda: self.now)

    def authorize(self, reservation):
        return authorize_automatic(self.sessions, reservation, lambda: self.now)

    def navigation(self, event):
        with self.sessions() as session:
            occurrence = session.get(Event, event)
            return card_navigation(session, occurrence, session.get(Alliance, occurrence.alliance_id))

    def competing_processes(self, mode, **values):
        payload = json.dumps({'now': self.now.isoformat(), **values})
        children = [subprocess.Popen([sys.executable, '-c', CLAIM_PROCESS, self.url, mode, payload],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) for _ in range(2)]
        try:
            for child in children:
                self.assertEqual(child.stdout.readline().strip(), 'ready')
            for child in children:
                child.stdin.write('go\n'); child.stdin.flush()
            results = []
            for child in children:
                stdout, stderr = child.communicate(timeout=30)
                self.assertEqual(child.returncode, 0, stderr)
                results.append(int(stdout.strip()))
            self.assertEqual(sorted(results), [0, 1])
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill(); child.communicate()

    def test_separate_process_publication_claim_and_authorization(self):
        self.configure(auto_publish=True); event = self.once()
        self.competing_processes('auto_claim', event=event)
        with self.sessions() as session:
            reservation = session.get(AutomaticPublication, event).publication_id
        self.competing_processes('auto_authorize', reservation=reservation)

    def test_separate_process_dm_claim_and_authorization(self):
        self.configure(dm_1h=True); event = self.once(); member = self.member()
        self.competing_processes('player_claim', event=event, member=member)
        with self.sessions() as session:
            claim = session.get(PlayerReminder, (event, 30, 60))
            delivery = asdict(PlayerDelivery(event, member, 30, 1, 1, 'Alpha', 'Duel', self.start, 60, claim.claim_token))
        delivery['starts_at'] = self.start.isoformat()
        self.competing_processes('player_authorize', delivery=delivery)

    async def test_bounded_publication_scan_does_not_starve_later_alliance(self):
        self.configure(auto_publish=True)
        for _ in range(51): self.once()
        later = self.once(alliance_id=2)
        configure_delivery(self.sessions, 1, 'Bravo', 40, False, auto_publish=True)
        self.channels.clear(); available = self.add_channel(102)
        await self.cards.publish_automatic()
        available.send.assert_not_awaited()
        await self.cards.publish_automatic()
        available.send.assert_awaited_once()
        self.assertEqual(self.publications()[0].event_id, later)
        # A repair is discovered after the cursor wraps; no failed head can stick.
        self.channels[101] = self.channel
        await self.cards.publish_automatic()
        self.assertEqual(self.channel.send.await_count, 50)
        await self.cards.publish_automatic()
        self.assertEqual(self.channel.send.await_count, 51)

    async def test_bounded_dm_scan_advances_past_terminal_attempts(self):
        self.configure(dm_1h=True); self.once()
        with self.sessions() as session:
            for user in range(100, 112):
                session.add(Member(alliance_id=1, game_name=str(user), discord_user_id=user, rank='R1'))
            session.commit()
        with patch('lastz_bot.player_reminders.PLAYER_REMINDER_BATCH', 5):
            for _ in range(4): await self.processor.process_pending()
            self.assertEqual(self.dm.await_count, 16)
            for _ in range(5): await self.processor.process_pending()
            self.assertEqual(self.dm.await_count, 16)

    async def test_three_delivery_ledgers_and_policies_remain_independent(self):
        from lastz_bot.reminders import ReminderProcessor
        from lastz_bot.rsvp_reminders import RSVPReminderProcessor
        event = self.once('required'); self.configure(dm_1h=True)
        self.edit_event(event, rsvp_deadline='2026-09-22 16:50', missing_reminder=True)
        channel = ReminderProcessor(self.sessions, self.dm, lambda: self.now).claim(event)
        normal = self.processor.claim(event, self.member())
        missing_processor = RSVPReminderProcessor(self.sessions, self.dm, lambda: self.now)
        missing = missing_processor.claim(event, self.member())
        self.assertIsNotNone(channel); self.assertIsNotNone(normal); self.assertIsNotNone(missing)
        message = await self.publish(event)
        # Disabling normal DMs cannot revoke either old mechanism's claim.
        self.configure(dm_1h=False)
        self.assertIsNone(self.processor.authorize_delivery(normal))
        authorized = missing_processor.authorize_delivery(missing)
        self.assertEqual(authorized.navigation, message.jump_url)
        self.assertIsNotNone(ReminderProcessor(self.sessions, self.dm).current_delivery(channel))

    async def test_normal_dm_send_failure_and_success_record_failure_are_terminal(self):
        self.configure(dm_1h=True); event = self.once()
        self.dm.side_effect = discord.HTTPException(Mock(status=403), 'blocked')
        await self.processor.process_pending()
        count = self.dm.await_count
        restarted = PlayerReminderProcessor(self.sessions, self.processor.send, lambda: self.now)
        await restarted.process_pending(); self.assertEqual(self.dm.await_count, count)
        event = self.once(); self.dm.side_effect = None
        with patch.object(self.processor, 'mark_sent', side_effect=SQLAlchemyError('recording')):
            with self.assertRaises(SQLAlchemyError): await self.processor.process_pending()
        with self.sessions() as session:
            claim = session.scalar(select(PlayerReminder).where(PlayerReminder.event_id == event))
            self.assertEqual(claim.status, 'attempted')
            self.assertIsNone(self.processor.claim(event, claim.member_id))

    def test_weekly_audience_away_back_revokes_player_claim(self):
        from lastz_bot.recurrence import edit_series
        self.configure(dm_1h=True); series = self.series(); event = self.rows(series)[0].id
        claim = self.processor.claim(event, self.member())
        edit_series(self.sessions, 1, series, 10, False, audience='R4', now=self.now)
        edit_series(self.sessions, 1, series, 10, False, audience='Everyone', now=self.now)
        self.assertIsNone(self.processor.authorize_delivery(claim))

    async def test_defaults_are_disabled_despite_existing_channel(self):
        self.once()
        self.assertEqual(self.configure(), (False, 0))
        await self.cards.publish_automatic(); await self.processor.process_pending()
        self.channel.send.assert_not_awaited(); self.dm.assert_not_awaited()

    async def test_auto_once_restart_and_manual_publication_independence(self):
        event = self.once(); self.configure(auto_publish=True)
        await self.cards.publish_automatic(); await self.cards.refresh()
        self.assertEqual(len(self.publications()), 1)
        self.assertIsNotNone(self.messages[1000].view)
        await EventCards(self.client, self.sessions).publish_automatic()
        self.assertEqual(self.channel.send.await_count, 1)
        await self.publish(event)
        self.assertEqual(len(self.publications()), 2)
        self.edit_event(event, name='Edited', description='New')
        await self.cards.refresh()
        self.assertTrue(all(m.embed.title == 'Edited' for m in self.messages.values()))
        self.assertTrue(all('Your local time' in self.fields(m) for m in self.messages.values()))

    async def test_weekly_horizon_past_backfill_and_stopped_series(self):
        self.configure(auto_publish=True)
        self.series(start=self.now - timedelta(days=800))
        far = self.series(start=self.now + timedelta(days=8))
        stopped = self.series()
        stop_series(self.sessions, 1, stopped, 10, False, now=self.now)
        await self.cards.publish_automatic()
        self.assertEqual(self.channel.send.await_count, 1)
        with self.sessions() as session:
            event = session.get(Event, self.publications()[0].event_id)
            self.assertGreater(event.starts_at, self.now)
            self.assertLessEqual(event.starts_at, self.now + timedelta(days=7))
        self.now += timedelta(days=1)
        ensure_occurrences(self.sessions, self.now)
        await self.cards.publish_automatic()
        self.assertIn(self.rows(far)[0].id, [p.event_id for p in self.publications()])

    async def test_missing_channel_permissions_then_repair_are_safe(self):
        event = self.once(); self.configure(auto_publish=True)
        self.channels.clear()
        await self.cards.publish_automatic(); self.channel.send.assert_not_awaited()
        self.channels[101] = self.channel
        self.channel.permissions_for.return_value.embed_links = False
        await self.cards.publish_automatic(); self.channel.send.assert_not_awaited()
        self.channel.permissions_for.return_value.embed_links = True
        await self.cards.publish_automatic()
        self.assertEqual(self.publications()[0].event_id, event)

    async def test_uncertain_send_terminal_even_after_reservation_cleanup(self):
        event = self.once(); self.configure(auto_publish=True)
        self.channel.send.side_effect = discord.HTTPException(Mock(status=500), 'uncertain')
        await self.cards.publish_automatic(); await self.cards.publish_automatic()
        self.channel.send.assert_awaited_once()
        with self.sessions() as session:
            attempt = session.get(AutomaticPublication, event)
            self.assertTrue(attempt.attempted); self.assertIsNone(attempt.publication_id)
            self.assertIsNotNone(session.get(Event, event))

    async def test_restart_before_send_and_after_send_before_registration(self):
        self.configure(auto_publish=True)
        for sent in (False, True):
            event = self.once(); reservation = self.reserve(event)
            if sent:
                self.assertIsNotNone(self.authorize(reservation))
                await self.channel.send(embed=discord.Embed(title='Inert'))
            count = self.channel.send.await_count
            await EventCards(self.client, self.sessions).publish_automatic()
            self.assertEqual(self.channel.send.await_count, count)
            abandon_publication(self.sessions, reservation)
            self.assertIsNone(self.reserve(event))
            self.assertFalse(register_automatic(self.sessions, reservation, 9999))

    async def test_pruning_pending_auto_reservation_does_not_retry(self):
        event = self.once(); self.configure(auto_publish=True)
        reservation = self.reserve(event)
        self.now += timedelta(hours=2)
        prune_pending_publications(self.sessions)
        self.now -= timedelta(hours=2)
        self.assertIsNone(self.reserve(event)); self.assertIsNone(self.authorize(reservation))

    async def test_two_workers_reserve_and_authorize_once(self):
        event = self.once(); self.configure(auto_publish=True)
        with ThreadPoolExecutor(2) as pool:
            reservations = list(pool.map(lambda _: self.reserve(event), range(2)))
        self.assertEqual(sum(r is not None for r in reservations), 1)
        reservation = next(r for r in reservations if r is not None)
        with ThreadPoolExecutor(2) as pool:
            states = list(pool.map(lambda _: self.authorize(reservation), range(2)))
        self.assertEqual(sum(s is not None for s in states), 1)

    async def test_overlapping_auto_workers_send_once(self):
        self.once(); self.configure(auto_publish=True)
        await asyncio.gather(self.cards.publish_automatic(), EventCards(self.client, self.sessions).publish_automatic())
        self.channel.send.assert_awaited_once()

    async def test_deletion_id_reuse_during_send_never_binds_replacement(self):
        event = self.once(); self.configure(auto_publish=True)
        original_send = self.channel.send.side_effect
        async def send(**kwargs):
            delete_event(self.sessions, 1, event, 10, False)
            replacement = self.once()
            self.assertEqual(replacement, event)
            return await original_send(**kwargs)
        self.channel.send.side_effect = send
        await self.cards.publish_automatic()
        self.assertEqual(self.publications(), [])
        self.messages[1000].delete.assert_awaited_once()
        self.assertIsNone(self.messages[1000].initial_view)

    async def test_database_registration_failure_deletes_inert_message_no_retry(self):
        self.once(); self.configure(auto_publish=True)
        with patch('lastz_bot.event_cards.register_automatic', side_effect=SQLAlchemyError('failure')):
            await self.cards.publish_automatic()
        self.messages[1000].delete.assert_awaited_once()
        await self.cards.publish_automatic(); self.channel.send.assert_awaited_once()

    async def test_auto_edit_failure_retries_edit_never_send(self):
        self.once(); self.configure(auto_publish=True)
        await self.cards.publish_automatic()
        message = self.messages[1000]
        message.edit.side_effect = discord.HTTPException(Mock(status=500), 'edit')
        await self.cards.refresh(); await self.cards.publish_automatic()
        self.channel.send.assert_awaited_once()
        message.edit.side_effect = None
        await self.cards.refresh()
        self.assertEqual(message.edit.await_count, 2)

    async def test_card_delete_does_not_republish_and_event_delete_tombstones(self):
        event = self.once(); self.configure(auto_publish=True)
        await self.cards.publish_automatic()
        delete_event(self.sessions, 1, event, 10, False)
        self.assertIsNone(self.publications()[0].event_id)
        await self.cards.refresh()
        self.assertEqual(self.messages[1000].embed.title, 'Event unavailable')
        event = self.once(); await self.cards.publish_automatic()
        self.cards._forget(self.publications()[0].message_id)
        await self.cards.publish_automatic()
        self.assertEqual(self.channel.send.await_count, 2)

    def test_discovery_exact_rank_tenant_active_and_multiple_alliances(self):
        alpha = self.once(); bravo = self.once(alliance_id=2); self.once(alliance_id=3)
        with self.sessions() as session:
            session.get(Event, alpha).audience = 8
            session.add(Member(alliance_id=2, game_name='Other membership', discord_user_id=10, rank='R1'))
            session.commit()
        rows, _ = personal_events(self.sessions, 1, 10, self.now)
        self.assertEqual({e.id for e, _, _ in rows}, {alpha, bravo})
        self.assertEqual(personal_events(self.sessions, 1, 20, self.now)[0], [])
        self.assertEqual(personal_events(self.sessions, 1, 999, self.now)[0], [])
        with self.sessions() as session:
            session.get(Member, self.member(10)).active = False; session.commit()
        self.assertEqual([e.id for e, _, _ in personal_events(self.sessions, 1, 10, self.now)[0]], [bravo])

    async def test_personal_commands_own_identity_bounded_ephemeral(self):
        for _ in range(9): self.once()
        for mode, count in (('mine', 5), ('next', 1), ('today', 5)):
            interaction = self.interaction(actor=30)
            await self.commands[mode](interaction)
            message = interaction.response.send_message.call_args
            self.assertEqual(message.args[0].count('ID `'), count)
            self.assertIn('More upcoming', message.args[0])
            self.assertLess(len(message.args[0]), 2000)
            self.assertTrue(message.kwargs['ephemeral'])
        interaction = self.interaction(actor=999, admin=True)
        await self.commands['mine'](interaction)
        self.assertIn('No upcoming', interaction.response.send_message.call_args.args[0])

    def test_today_uses_at_day_not_utc_day(self):
        self.now = datetime(2026, 9, 23, 0, 30)
        ids = []
        for hour in (1, 2, 3):
            self.start = datetime(2026, 9, 23, hour); ids.append(self.once())
        rows, _ = personal_events(self.sessions, 1, 30, self.now, 'today')
        self.assertEqual([e.id for e, _, _ in rows], ids[:1])

    def test_timestamp_is_canonical_utc(self):
        epoch = int(self.start.replace(tzinfo=timezone.utc).timestamp())
        self.assertEqual(discord_timestamp(self.start), f'<t:{epoch}:F> (<t:{epoch}:R>)')
        self.assertEqual(utc_to_apocalypse_time(self.start).hour, 17)

    async def test_navigation_prefers_configured_channel_then_stable_id_and_fallback(self):
        event = self.once()
        self.assertEqual(self.navigation(event), f'`/event rsvp event_id:{event}`')
        other = self.add_channel(102)
        message = await self.publish(event, channel=other)
        self.assertEqual(self.navigation(event), message.jump_url)
        preferred = await self.publish(event)
        await self.publish(event)
        self.assertEqual(self.navigation(event), preferred.jump_url)
        self.cards._forget(preferred.id)
        self.assertNotEqual(self.navigation(event), preferred.jump_url)

    async def test_all_supported_dm_leads_and_latest_only_catchup(self):
        self.configure(dm_24h=True, dm_1h=True, dm_15m=True)
        event = self.once(); member = self.member()
        for lead in (1440, 60, 15):
            self.now = self.start - timedelta(minutes=lead)
            delivery = self.processor.claim(event, member)
            self.assertEqual(delivery.lead_minutes, lead)
            self.assertIsNotNone(self.processor.authorize_delivery(delivery))
            self.processor.mark_sent(delivery)
            self.assertIsNone(self.processor.claim(event, member))
        event = self.once(); self.now = self.start - timedelta(minutes=5)
        self.assertEqual(self.processor.claim(event, member).lead_minutes, 15)
        with self.sessions() as session:
            self.assertEqual(dict(session.execute(select(PlayerReminder.lead_minutes, PlayerReminder.status)
                .where(PlayerReminder.event_id == event)).all()), {1440: 'skipped', 60: 'skipped', 15: 'claimed'})

    def test_dm_boundary_no_early_or_expired(self):
        for remaining, expected in ((1441, None), (1440, 1440), (61, 1440), (60, 60), (16, 60), (15, 15), (0, None), (-1, None)):
            self.assertEqual(due_lead(7, self.start, self.start-timedelta(minutes=remaining)), expected)
        self.assertIsNone(due_lead(0, self.start, self.now))

    async def test_dm_exact_audience_inactive_unlinked_and_rsvp_independence(self):
        event = self.once(); self.configure(dm_1h=True)
        self.edit_event(event, audience='R1')
        self.change(active=False)
        with self.sessions() as session:
            session.add(Member(alliance_id=1, game_name='Unlinked', rank='R1')); session.commit()
        from lastz_bot.rsvp import set_rsvp
        set_rsvp(self.sessions, 1, event, 50, 'not_going')
        self.once(alliance_id=2); self.once(alliance_id=3)
        await self.processor.process_pending()
        self.assertEqual([c.args[0].discord_user_id for c in self.dm.await_args_list], [50])

    async def test_restart_after_claim_and_uncertain_attempt_no_retry(self):
        event = self.once(); self.configure(dm_1h=True)
        claim = self.processor.claim(event, self.member())
        self.connect()
        restarted = PlayerReminderProcessor(self.sessions, self.dm, lambda: self.now)
        self.assertIsNone(restarted.claim(event, self.member()))
        self.assertIsNotNone(restarted.authorize_delivery(claim))
        self.assertIsNone(restarted.authorize_delivery(claim))
        self.assertIsNone(restarted.claim(event, self.member()))

    def test_member_rank_activity_and_link_away_back_revoke_claim(self):
        self.configure(dm_1h=True)
        for first, second in (({'rank':'R3'}, {'rank':'R1'}), ({'active':False}, {'active':True}),
                              ({'link_to':333}, {'link_to':30})):
            event = self.once(); claim = self.processor.claim(event, self.member())
            member = self.member()
            self.change(**first)
            if 'link_to' in first:
                change_member(self.sessions, 1, 'Alpha', 999, True, discord_user_id=333, **second)
            else: self.change(**second)
            self.assertIsNone(self.processor.authorize_delivery(claim))
            self.assertIsNone(self.processor.claim(event, member))

    def test_audience_schedule_policy_away_back_and_noop(self):
        self.configure(dm_1h=True)
        for mutation in ('audience', 'time', 'policy'):
            event = self.once(); claim = self.processor.claim(event, self.member())
            if mutation == 'audience':
                self.edit_event(event, audience='R4'); self.edit_event(event, audience='Everyone')
            elif mutation == 'time':
                self.edit_event(event, starts_at='2026-09-22 18:00'); self.edit_event(event, starts_at='2026-09-22 17:00')
            else:
                self.configure(dm_1h=False); self.configure(dm_1h=True)
            self.assertIsNone(self.processor.authorize_delivery(claim))
        event = self.once(); claim = self.processor.claim(event, self.member())
        self.configure(dm_1h=True); self.edit_event(event, name='Text only')
        self.assertEqual(self.processor.authorize_delivery(claim).event_name, 'Text only')

    def test_delete_id_reuse_and_series_stop_invalidate_dm(self):
        self.configure(dm_1h=True)
        event = self.once(); claim = self.processor.claim(event, self.member())
        delete_event(self.sessions, 1, event, 10, False)
        self.assertEqual(self.once(), event)
        new = self.processor.claim(event, self.member())
        self.assertIsNone(self.processor.authorize_delivery(claim))
        self.processor.mark_sent(claim)
        self.assertIsNotNone(self.processor.authorize_delivery(new))
        series = self.series(); event = self.rows(series)[0].id
        claim = self.processor.claim(event, self.member())
        stop_series(self.sessions, 1, series, 10, False, now=self.now)
        self.assertIsNone(self.processor.authorize_delivery(claim))

    def test_overlapping_dm_claims_and_authorizations(self):
        self.configure(dm_1h=True); event = self.once(); member = self.member()
        with ThreadPoolExecutor(2) as pool:
            claims = list(pool.map(lambda _: self.processor.claim(event, member), range(2)))
        self.assertEqual(sum(c is not None for c in claims), 1)
        claim = next(c for c in claims if c is not None)
        with ThreadPoolExecutor(2) as pool:
            deliveries = list(pool.map(lambda _: self.processor.authorize_delivery(claim), range(2)))
        self.assertEqual(sum(d is not None for d in deliveries), 1)

    async def test_delayed_lookup_rechecks_membership_before_send(self):
        self.configure(dm_1h=True); event = self.once()
        worker = ReminderWorker(self.client); worker.player_processor = self.processor
        self.client.get_user.return_value = None
        user = SimpleNamespace(id=30, create_dm=AsyncMock(return_value=SimpleNamespace(send=self.dm)))
        async def fetch(user_id):
            self.change(active=False)
            return user
        self.client.fetch_user = AsyncMock(side_effect=fetch)
        claim = self.processor.claim(event, self.member())
        with self.assertRaises(RuntimeError): await worker.send_player(claim)
        self.dm.assert_not_awaited()

    async def test_transport_dm_local_time_and_card_link(self):
        self.configure(dm_1h=True); event = self.once(); message = await self.publish(event)
        worker = ReminderWorker(self.client); worker.player_processor = self.processor
        self.client.get_user.return_value = SimpleNamespace(id=30, create_dm=AsyncMock(return_value=SimpleNamespace(send=self.dm)))
        await worker.send_player(self.processor.claim(event, self.member()))
        content = self.dm.call_args.args[0]
        self.assertIn('17:00 AT', content); self.assertIn('<t:', content); self.assertIn(message.jump_url, content)

    def test_configuration_permissions_and_tenant_scope(self):
        for actor, guild, alliance, admin, allowed in (
            (10,1,'Alpha',False,True), (20,1,'Alpha',False,True), (30,1,'Alpha',False,False),
            (999,1,'Alpha',False,False), (10,1,'Bravo',False,False), (10,2,'Alpha',False,False),
            (999,1,'Alpha',True,True),
        ):
            if allowed:
                configure_delivery(self.sessions, guild, alliance, actor, admin, auto_publish=True)
            else:
                with self.assertRaises(EventManagementError):
                    configure_delivery(self.sessions, guild, alliance, actor, admin, auto_publish=True)
        change_member(self.sessions,1,'Alpha',999,True,discord_user_id=10,active=False)
        with self.assertRaises(EventManagementError): self.configure(dm_1h=True)
        with self.sessions() as session:
            self.assertFalse(session.get(Alliance, 2).auto_publish)
            self.assertFalse(session.get(Alliance, 3).auto_publish)

    async def test_delivery_command_and_rank_choices_audience_autocomplete(self):
        tree = Mock(); setup_alliance_commands(tree)
        command = tree.add_command.call_args.args[0].get_command('event-delivery')
        with patch('lastz_bot.commands.alliance.SessionLocal', self.sessions):
            interaction = self.interaction()
            await command.callback(interaction, 'Alpha', True, False, True, False)
            self.assertIn('60m', interaction.response.send_message.call_args.args[0])
        tree = Mock(); setup_member_commands(tree)
        rank = tree.add_command.call_args.args[0].get_command('rank')
        self.assertEqual([c.value for c in rank.get_parameter('rank').choices], ['R1','R2','R3','R4','R5'])
        tree = Mock(); setup_event_commands(tree)
        create = tree.add_command.call_args.args[0].get_command('create')
        choices = await create._params['audience'].autocomplete(self.interaction(), 'R3')
        self.assertIn('R3', [c.value for c in choices]); self.assertLessEqual(len(choices), 25)
