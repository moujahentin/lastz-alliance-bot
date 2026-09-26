"""Independent, at-most-once normal event DMs; RSVP responses do not filter them.

Only the latest enabled due threshold is sent. Older enabled leads are skipped.
Reschedules revoke old authority but do not repeat attempted leads.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from uuid import uuid4

from sqlalchemy import or_, select, text, update

from lastz_bot.database.models import Alliance, Event, Member, PlayerReminder
from lastz_bot.event_time import utc_now_naive
from lastz_bot.player_events import card_navigation, eligible_events
from lastz_bot.player_policy import LEADS

logger = logging.getLogger(__name__)
PLAYER_REMINDER_BATCH = 100


def due_lead(mask, starts_at, now):
    remaining = starts_at - now
    if remaining <= timedelta(0):
        return None
    return next((lead for bit, lead in reversed(list(enumerate(LEADS)))
                 if mask & (1 << bit) and remaining <= timedelta(minutes=lead)), None)


@dataclass(frozen=True)
class PlayerDelivery:
    event_id: int
    member_id: int
    discord_user_id: int
    alliance_id: int
    guild_id: int
    alliance_name: str
    event_name: str
    starts_at: datetime
    lead_minutes: int
    claim_token: str
    navigation: str = ""


class PlayerReminderProcessor:
    def __init__(self, sessions, send, clock=utc_now_naive):
        self.sessions, self.send, self.clock = sessions, send, clock
        self.cursor = (0, 0)

    def eligible(self, session, event_id, member_id):
        row = session.execute(eligible_events().where(Event.id == event_id, Member.id == member_id)).first()
        if row is None:
            return None
        event, alliance, member = row
        lead = due_lead(alliance.player_reminder_mask, event.starts_at, self.clock())
        return (event, alliance, member, lead) if lead is not None else None

    def claim(self, event_id, member_id):
        with self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = self.eligible(session, event_id, member_id)
            if row is None:
                return None
            event, alliance, member, lead = row
            used = set(session.scalars(select(PlayerReminder.lead_minutes).where(
                PlayerReminder.event_id == event_id, or_(PlayerReminder.member_id == member_id,
                    PlayerReminder.discord_user_id == member.discord_user_id))))
            for bit, older in enumerate(LEADS):
                if older > lead and alliance.player_reminder_mask & (1 << bit) and older not in used:
                    session.add(PlayerReminder(event_id=event_id, member_id=member_id,
                        discord_user_id=member.discord_user_id, lead_minutes=older,
                        status="skipped", recorded_at=self.clock()))
            delivery = None
            if lead not in used:
                token = uuid4().hex
                session.add(PlayerReminder(event_id=event_id, member_id=member_id,
                    discord_user_id=member.discord_user_id, lead_minutes=lead, claim_token=token,
                    status="claimed", recorded_at=self.clock()))
                delivery = PlayerDelivery(event_id, member_id, member.discord_user_id, alliance.id,
                    alliance.guild_id, alliance.name, event.name, event.starts_at, lead, token)
            session.commit()
            return delivery

    def authorize_delivery(self, delivery):
        with self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            claim = session.get(PlayerReminder, (delivery.event_id, delivery.discord_user_id, delivery.lead_minutes))
            if (claim is None or claim.status != "claimed" or not claim.claim_token
                    or claim.claim_token != delivery.claim_token or claim.member_id != delivery.member_id):
                return None
            row = self.eligible(session, delivery.event_id, delivery.member_id)
            if row is None:
                return None
            event, alliance, member, lead = row
            if (alliance.id, alliance.guild_id, member.discord_user_id, event.starts_at, lead) != (
                    delivery.alliance_id, delivery.guild_id, delivery.discord_user_id, delivery.starts_at, delivery.lead_minutes):
                return None
            result = PlayerDelivery(event.id, member.id, member.discord_user_id, alliance.id,
                alliance.guild_id, alliance.name, event.name, event.starts_at, lead,
                delivery.claim_token, card_navigation(session, event, alliance))
            claim.status = "attempted"
            session.commit()
            return result

    def mark_sent(self, delivery):
        with self.sessions() as session:
            session.execute(update(PlayerReminder).where(
                PlayerReminder.event_id == delivery.event_id,
                PlayerReminder.discord_user_id == delivery.discord_user_id,
                PlayerReminder.lead_minutes == delivery.lead_minutes,
                PlayerReminder.claim_token == delivery.claim_token,
                PlayerReminder.status == "attempted",
            ).values(status="sent", sent_at=self.clock()))
            session.commit()

    async def process_pending(self):
        with self.sessions() as session:
            now = self.clock()
            query = eligible_events().where(Alliance.player_reminder_mask != 0,
                Event.starts_at > now, Event.starts_at <= now + timedelta(days=1))
            page = query.where(or_(Event.id > self.cursor[0],
                (Event.id == self.cursor[0]) & (Member.id > self.cursor[1])))
            rows = session.execute(page.order_by(Event.id, Member.id).limit(PLAYER_REMINDER_BATCH)).all()
            if not rows and self.cursor != (0, 0):
                rows = session.execute(query.order_by(Event.id, Member.id).limit(PLAYER_REMINDER_BATCH)).all()
            candidates = [(event.id, member.id) for event, _, member in rows]
            # Fair bounded scanning, including rows whose attempts are terminal.
            # Restart may repeat a page but cannot repeat persisted deliveries.
            self.cursor = candidates[-1] if candidates else (0, 0)
        for event_id, member_id in candidates:
            delivery = self.claim(event_id, member_id)
            if delivery is None:
                continue
            try:
                await self.send(delivery)
            except Exception:
                logger.warning("Player reminder attempt did not complete: event=%s; no retry", event_id)
                continue
            self.mark_sent(delivery)
