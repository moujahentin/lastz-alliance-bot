"""Persistent, at-most-once reminder attempts for the SQLite event store.

Missing delivery rows are pending. Claims are committed BEFORE calling the
sender and never reclaimed, even after failure or restart. This deliberately
trades a possible lost message for suppressing duplicate application sends.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import logging
from uuid import uuid4

from sqlalchemy import exists, or_, select, text, update
from sqlalchemy.orm import sessionmaker

from lastz_bot.database.models import Alliance, Event, EventReminder, EventSeries
from lastz_bot.event_time import utc_now_naive
from lastz_bot.recurrence import ensure_occurrences


logger = logging.getLogger(__name__)


def active_occurrence():
    return (Event.status == "scheduled") & or_(
        Event.series_id.is_(None),
        exists().where(EventSeries.id == Event.series_id, EventSeries.active.is_(True)),
    )


def eligible_threshold(starts_at: datetime, now: datetime) -> int | None:
    """Use UTC-naive values; only the latest due threshold can be attempted."""
    remaining = starts_at - now
    if remaining <= timedelta(0) or remaining > timedelta(minutes=30):
        return None
    return 10 if remaining <= timedelta(minutes=10) else 30


@dataclass(frozen=True)
class ReminderDelivery:
    event_id: int
    alliance_id: int
    guild_id: int
    channel_id: int
    alliance_name: str
    event_name: str
    starts_at: datetime
    lead_minutes: int
    claim_token: str


class ReminderProcessor:
    def __init__(
        self,
        sessions: sessionmaker,
        send: Callable[[ReminderDelivery], Awaitable[None]],
        clock: Callable[[], datetime] = utc_now_naive,
    ) -> None:
        self.sessions = sessions
        self.send = send
        self.clock = clock

    def claim(self, event_id: int) -> ReminderDelivery | None:
        """Serialize claims with SQLite's write lock, never across network I/O.

        The composite primary key also enforces one record per opportunity.
        Resolve the event's actual alliance/guild and current channel together;
        no user-supplied tenant or channel IDs participate in this lookup.
        """
        with self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.execute(
                select(Event, Alliance)
                .join(Alliance, Event.alliance_id == Alliance.id)
                .where(Event.id == event_id, active_occurrence())
            ).first()
            if row is None:
                return None
            event, alliance = row
            now = self.clock()
            lead = eligible_threshold(event.starts_at, now)
            if lead is None:
                return None

            if lead == 10 and session.get(EventReminder, (event.id, 30)) is None:
                # Persist this even without a channel or when 10 was attempted.
                # Never fall back to an older threshold on subsequent cycles.
                session.add(EventReminder(
                    event_id=event.id, lead_minutes=30, status="skipped",
                    recorded_at=now,
                ))

            delivery = None
            if (
                alliance.reminder_channel_id is not None
                and session.get(EventReminder, (event.id, lead)) is None
            ):
                claim_token = uuid4().hex
                session.add(EventReminder(
                    event_id=event.id, lead_minutes=lead, status="claimed",
                    channel_id=alliance.reminder_channel_id, recorded_at=now,
                    claim_token=claim_token,
                ))
                delivery = ReminderDelivery(
                    event.id, alliance.id, alliance.guild_id,
                    alliance.reminder_channel_id, alliance.name, event.name,
                    event.starts_at, lead, claim_token,
                )
            session.commit()
            return delivery

    def current_delivery(self, delivery: ReminderDelivery) -> ReminderDelivery | None:
        """Reject deleted/reset claims and refresh event text before sending.

        Tokens also prevent stale work from becoming valid when a schedule is
        changed away and back, or SQLite reuses a deleted event's integer ID.
        """
        with self.sessions() as session:
            row = session.execute(
                select(Event, Alliance)
                .join(Alliance, Event.alliance_id == Alliance.id)
                .join(EventReminder, EventReminder.event_id == Event.id)
                .where(
                    Event.id == delivery.event_id,
                    active_occurrence(),
                    Event.alliance_id == delivery.alliance_id,
                    Alliance.guild_id == delivery.guild_id,
                    Event.starts_at == delivery.starts_at,
                    EventReminder.lead_minutes == delivery.lead_minutes,
                    EventReminder.claim_token == delivery.claim_token,
                    EventReminder.channel_id == delivery.channel_id,
                    EventReminder.status == "claimed",
                )
            ).first()
            if row is None:
                return None
            event, alliance = row
            return replace(delivery, event_name=event.name, alliance_name=alliance.name)

    def mark_sent(self, delivery: ReminderDelivery) -> None:
        """An old in-flight send must never mark a replacement claim as sent."""
        with self.sessions() as session:
            session.execute(
                update(EventReminder).where(
                    EventReminder.event_id == delivery.event_id,
                    EventReminder.lead_minutes == delivery.lead_minutes,
                    EventReminder.claim_token == delivery.claim_token,
                    EventReminder.status == "claimed",
                ).values(status="sent", sent_at=self.clock())
            )
            session.commit()

    async def process_pending(self) -> None:
        now = self.clock()
        ensure_occurrences(self.sessions, now)
        with self.sessions() as session:
            event_ids = session.scalars(
                select(Event.id).where(
                    Event.starts_at > now,
                    active_occurrence(),
                    Event.starts_at <= now + timedelta(minutes=30),
                ).order_by(Event.starts_at, Event.id)
            ).all()

        for event_id in event_ids:
            # Re-read time and configuration per event, after any prior send.
            delivery = self.claim(event_id)
            if delivery is None:
                continue
            try:
                await self.send(delivery)
            except Exception:
                # Includes uncertain HTTP failures. Keep the claim, never retry.
                # Cancellation/process death also leaves the committed claim.
                logger.warning(
                    "Reminder attempt did not complete: event=%s lead=%s; no retry",
                    delivery.event_id, delivery.lead_minutes,
                )
                continue
            self.mark_sent(delivery)
