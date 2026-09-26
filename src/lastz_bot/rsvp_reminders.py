"""Targeted RSVP attempts run in the existing reminder worker, never a new timer.

Claims are terminal even after DM failure, cancellation, or restart. Edits never
reset an attempt. A final serialized check happens after Discord DM resolution.
"""
from dataclasses import dataclass
from datetime import datetime
import logging
from uuid import uuid4

from sqlalchemy import or_, select, text, update

from lastz_bot.database.models import Alliance, Event, RSVPReminder
from lastz_bot.event_time import utc_now_naive
from lastz_bot.nonresponders import nonresponders
from lastz_bot.player_events import card_navigation
from lastz_bot.reminders import active_occurrence
from lastz_bot.rsvp_policy import MISSING_REMINDER_LEAD

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RSVPReminderDelivery:
    event_id: int
    member_id: int
    discord_user_id: int
    alliance_id: int
    guild_id: int
    alliance_name: str
    event_name: str
    starts_at: datetime
    deadline: datetime
    claim_token: str
    navigation: str = ""


class RSVPReminderProcessor:
    def __init__(self, sessions, send, clock=utc_now_naive):
        self.sessions, self.send, self.clock = sessions, send, clock

    def _eligible(self, session, event_id, member_id):
        row = session.execute(select(Event, Alliance).join(Alliance).where(
            Event.id == event_id, active_occurrence(),
            Event.participation == 'required', Event.missing_reminder.is_(True),
        )).first()
        if row is None:
            return None
        occurrence, alliance = row
        now = self.clock()
        deadline = occurrence.rsvp_deadline
        if (deadline is None or now >= deadline or deadline - now > MISSING_REMINDER_LEAD
                or now >= occurrence.starts_at):
            return None
        member = next((member for member in nonresponders(session, occurrence, alliance.guild_id)
                       if member.member_id == member_id and member.discord_user_id is not None), None)
        return (occurrence, alliance, member) if member is not None else None

    def claim(self, event_id, member_id):
        with self.sessions() as session:
            session.execute(text('BEGIN IMMEDIATE'))
            row = self._eligible(session, event_id, member_id)
            if row is None:
                return None
            occurrence, alliance, member = row
            if session.scalar(select(RSVPReminder).where(RSVPReminder.event_id == event_id, or_(
                RSVPReminder.member_id == member_id, RSVPReminder.discord_user_id == member.discord_user_id,
            ))) is not None:
                return None
            token = uuid4().hex
            session.add(RSVPReminder(event_id=event_id, member_id=member_id,
                discord_user_id=member.discord_user_id, claim_token=token, status='claimed', recorded_at=self.clock()))
            delivery = RSVPReminderDelivery(event_id, member_id, member.discord_user_id,
                alliance.id, alliance.guild_id, alliance.name, occurrence.name,
                occurrence.starts_at, occurrence.rsvp_deadline, token)
            session.commit()
            return delivery

    def current_delivery(self, delivery, *, authorize=False):
        with self.sessions() as session:
            # RSVP, membership management, policy edits, and final authorization
            # serialize. Never retain this lock across Discord I/O.
            session.execute(text('BEGIN IMMEDIATE'))
            claim = session.get(RSVPReminder, (delivery.event_id, delivery.discord_user_id))
            if (claim is None or claim.status != 'claimed' or claim.claim_token is None
                    or claim.claim_token != delivery.claim_token
                    or claim.member_id != delivery.member_id):
                return None
            row = self._eligible(session, delivery.event_id, delivery.member_id)
            if row is None:
                return None
            occurrence, alliance, member = row
            if (alliance.id, alliance.guild_id, member.discord_user_id, occurrence.starts_at, occurrence.rsvp_deadline) != (
                delivery.alliance_id, delivery.guild_id, delivery.discord_user_id, delivery.starts_at, delivery.deadline,
            ):
                return None
            current = RSVPReminderDelivery(delivery.event_id, member.member_id, member.discord_user_id,
                alliance.id, alliance.guild_id, alliance.name, occurrence.name,
                occurrence.starts_at, occurrence.rsvp_deadline, delivery.claim_token,
                card_navigation(session, occurrence, alliance))
            if authorize:
                claim.status = 'attempted'
                session.commit()  # Before send: even replaying this delivery cannot send twice.
            return current

    def authorize_delivery(self, delivery):
        return self.current_delivery(delivery, authorize=True)

    def mark_sent(self, delivery):
        if not delivery.claim_token:
            return
        with self.sessions() as session:
            session.execute(update(RSVPReminder).where(
                RSVPReminder.event_id == delivery.event_id, RSVPReminder.discord_user_id == delivery.discord_user_id,
                RSVPReminder.member_id == delivery.member_id, RSVPReminder.claim_token == delivery.claim_token,
                RSVPReminder.status == 'attempted',
            ).values(status='sent', sent_at=self.clock()))
            session.commit()

    async def process_pending(self):
        # Occurrence generation runs once per existing worker cycle before this
        # processor, preserving its historical backfill budget.
        with self.sessions() as session:
            session.execute(text('BEGIN'))
            now = self.clock()
            rows = session.execute(select(Event, Alliance).join(Alliance).where(
                active_occurrence(), Event.participation == 'required', Event.missing_reminder.is_(True),
                Event.rsvp_deadline > now, Event.rsvp_deadline <= now + MISSING_REMINDER_LEAD,
                Event.starts_at > now,
            ).order_by(Event.id)).all()
            candidates = [(occurrence.id, member.member_id) for occurrence, alliance in rows
                          for member in nonresponders(session, occurrence, alliance.guild_id)
                          if member.discord_user_id is not None]
        for event_id, member_id in candidates:
            delivery = self.claim(event_id, member_id)
            if delivery is None:
                continue
            try:
                await self.send(delivery)
            except Exception:
                # No success is recorded. Keep even uncertain failures terminal.
                logger.warning('RSVP reminder attempt did not complete: event=%s; no retry', event_id)
                continue
            self.mark_sent(delivery)
