"""Persistent card associations and database-authoritative display snapshots."""
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import sessionmaker

from lastz_bot.database.models import Alliance, Event, EventPublication, EventRSVP, EventSeries
from lastz_bot.event_management import EventManagementError, _managed_event
from lastz_bot.event_time import utc_now_naive


@dataclass(frozen=True)
class CardState:
    event_id: int
    name: str
    description: str | None
    starts_at: datetime
    alliance: str
    series_id: int | None
    participation: str
    closed: bool
    status: str
    audience: int
    counts: tuple[int, int, int]  # Going, Maybe, Not Going.


def read_card(sessions: sessionmaker, event_id: int, guild_id: int) -> CardState | None:
    with sessions() as session:
        session.execute(text("BEGIN"))
        return _read_card(session, event_id, guild_id)


def read_publication_card(sessions: sessionmaker, message_id: int):
    # Binding and occurrence must share a snapshot: never retarget a card after
    # deletion followed by SQLite integer ID reuse during a refresh.
    with sessions() as session:
        session.execute(text("BEGIN"))
        publication = session.scalar(select(EventPublication).where(EventPublication.message_id == message_id))
        if publication is None:
            return None, None
        state = _read_card(session, publication.event_id, publication.guild_id) if publication.event_id is not None else None
        return publication, state


def _read_card(session, event_id: int, guild_id: int) -> CardState | None:
    row = session.execute(select(Event, Alliance).join(Alliance, Event.alliance_id == Alliance.id).where(
        Event.id == event_id, Alliance.guild_id == guild_id,
    )).first()
    if row is None:
        return None
    occurrence, alliance = row
    series = session.get(EventSeries, occurrence.series_id) if occurrence.series_id is not None else None
    status = occurrence.status
    if series is not None and not series.active:
        status = "stopped"
    elif status == "scheduled" and occurrence.starts_at <= utc_now_naive():
        status = "expired"
    counts = dict(session.execute(select(EventRSVP.response, func.count()).where(
        EventRSVP.event_id == event_id,
    ).group_by(EventRSVP.response)).all())
    return CardState(
        occurrence.id, occurrence.name, occurrence.description, occurrence.starts_at,
        alliance.name, occurrence.series_id, occurrence.participation,
        status != "scheduled" or occurrence.participation == "none", status,
        occurrence.audience,
        tuple(counts.get(key, 0) for key in ("going", "maybe", "not_going")),
    )


def authorize_publish(sessions: sessionmaker, guild_id: int, event_id: int,
                      actor_id: int, administrator: bool) -> None:
    with sessions() as session:
        _managed_event(session, guild_id, event_id, actor_id, administrator)


def reserve_publication(sessions: sessionmaker, guild_id: int, event_id: int,
                        channel_id: int, actor_id: int, administrator: bool) -> tuple[int, CardState]:
    with sessions() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        _managed_event(session, guild_id, event_id, actor_id, administrator)
        state = _read_card(session, event_id, guild_id)
        publication = EventPublication(event_id=event_id, guild_id=guild_id, channel_id=channel_id,
                                       created_at=utc_now_naive())
        session.add(publication)
        session.flush()
        publication_id = publication.id
        session.commit()
        return publication_id, state


def record_publication(sessions: sessionmaker, publication_id: int, message_id: int,
                       actor_id: int, administrator: bool) -> None:
    with sessions() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        publication = session.get(EventPublication, publication_id)
        if publication is None or publication.event_id is None or publication.message_id is not None:
            raise EventManagementError("❌ This event is no longer available.")
        _managed_event(session, publication.guild_id, publication.event_id, actor_id, administrator)
        publication.message_id = message_id
        session.commit()


def resolve_publication(sessions: sessionmaker, guild_id: int, channel_id: int, message_id: int) -> int:
    with sessions() as session:
        occurrence = session.scalar(select(Event.id).join(Alliance, Event.alliance_id == Alliance.id).join(
            EventPublication, EventPublication.event_id == Event.id,
        ).where(EventPublication.message_id == message_id, EventPublication.channel_id == channel_id,
                EventPublication.guild_id == guild_id, Alliance.guild_id == guild_id))
        if occurrence is None:
            raise EventManagementError("❌ This event card is no longer available.")
        return occurrence


PENDING_PUBLICATION_TTL = timedelta(hours=1)


def abandon_publication(sessions: sessionmaker, publication_id: int) -> None:
    with sessions() as session:
        session.execute(delete(EventPublication).where(EventPublication.id == publication_id))
        session.commit()


def prune_pending_publications(sessions: sessionmaker) -> None:
    """Only expire inert, unregistered attempts; never retry a Discord send.

    A very late completion finds no reservation and deletes its inert message.
    AUTOINCREMENT prevents it from binding to another publish's reservation.
    """
    with sessions() as session:
        session.execute(delete(EventPublication).where(
            EventPublication.message_id.is_(None),
            EventPublication.created_at <= utc_now_naive() - PENDING_PUBLICATION_TTL,
        ))
        session.commit()
