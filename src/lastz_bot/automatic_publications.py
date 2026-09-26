"""Automatic cards reuse PR7 reservations and keep a separate terminal ledger.

Deleting/pruning a card never grants a second automatic send. A crash after
reservation may lose this opportunity; manual /event publish is the recovery.
"""
from datetime import timedelta

from sqlalchemy import or_, select, text

from lastz_bot.database.models import Alliance, AutomaticPublication, Event, EventPublication
from lastz_bot.event_time import utc_now_naive
from lastz_bot.publications import _read_card
from lastz_bot.reminders import active_occurrence

PUBLICATION_HORIZON = timedelta(days=7)
PUBLICATION_BATCH = 50


def candidates(now):
    return select(Event, Alliance).join(Alliance).where(
        active_occurrence(), Event.starts_at > now, Alliance.auto_publish.is_(True),
        Alliance.reminder_channel_id.is_not(None),
        or_(Event.series_id.is_(None), Event.starts_at <= now + PUBLICATION_HORIZON),
        ~select(AutomaticPublication.event_id).where(AutomaticPublication.event_id == Event.id).exists(),
    )


def reserve_automatic(sessions, event_id, guild_id, channel_id, clock=utc_now_naive):
    with sessions() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        row = session.execute(candidates(clock()).where(Event.id == event_id,
            Alliance.guild_id == guild_id, Alliance.reminder_channel_id == channel_id)).first()
        if row is None:
            return None
        publication = EventPublication(event_id=event_id, guild_id=guild_id,
                                       channel_id=channel_id, created_at=clock())
        session.add(publication)
        session.flush()
        session.add(AutomaticPublication(event_id=event_id, publication_id=publication.id,
                                        attempted=False, recorded_at=clock()))
        result = publication.id
        session.commit()
        return result


def authorize_automatic(sessions, publication_id, clock=utc_now_naive):
    with sessions() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        row = session.execute(select(AutomaticPublication, EventPublication, Event, Alliance)
            .join(EventPublication, AutomaticPublication.publication_id == EventPublication.id)
            .join(Event, EventPublication.event_id == Event.id).join(Alliance, Event.alliance_id == Alliance.id)
            .where(EventPublication.id == publication_id, AutomaticPublication.event_id == Event.id,
                   AutomaticPublication.attempted.is_(False), EventPublication.message_id.is_(None),
                   Alliance.guild_id == EventPublication.guild_id,
                   Alliance.reminder_channel_id == EventPublication.channel_id,
                   Alliance.auto_publish.is_(True), active_occurrence(), Event.starts_at > clock(),
                   or_(Event.series_id.is_(None), Event.starts_at <= clock() + PUBLICATION_HORIZON))).first()
        if row is None:
            return None
        attempt, publication, event, alliance = row
        state = _read_card(session, event.id, alliance.guild_id)
        attempt.attempted = True
        session.commit()
        return state


def register_automatic(sessions, publication_id, message_id):
    with sessions() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        publication = session.get(EventPublication, publication_id)
        if publication is None or publication.event_id is None or publication.message_id is not None:
            return False
        attempt = session.get(AutomaticPublication, publication.event_id)
        if attempt is None or attempt.publication_id != publication.id or not attempt.attempted:
            return False
        publication.message_id = message_id
        session.commit()
        return True
