"""Tenant-scoped event changes and atomic reminder resets."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session, sessionmaker

from lastz_bot.database.models import Alliance, Event, EventReminder, EventAudienceChange
from lastz_bot.event_time import parse_apocalypse_time, utc_now_naive
from lastz_bot.rsvp_policy import parse_deadline, validate_deadline, invalidate_missing_claims
from lastz_bot.permissions import get_management_rank


class EventManagementError(ValueError):
    """A command-safe validation or access error."""


def validate_participation(value: str) -> None:
    if value not in {"none", "optional", "required"}:
        raise EventManagementError("❌ Participation must be none, optional, or required.")


def validate_one_time_start(starts_at: datetime, now: datetime) -> None:
    """Require a strictly future UTC-naive start, without rounding the clock.

    Minute-precision AT input in the current minute is already due. Call inside
    the write transaction so time spent waiting for its lock is accounted for.
    Weekly anchors intentionally bypass this policy to permit backfill.
    """
    if starts_at <= now:
        raise EventManagementError(
            "❌ One-time events must start in the future. Choose a later Apocalypse Time."
        )


def _managed_event(
    session: Session, guild_id: int, event_id: int, actor_id: int, administrator: bool,
) -> Event:
    row = session.execute(
        select(Event, Alliance)
        .join(Alliance, Event.alliance_id == Alliance.id)
        .where(Event.id == event_id, Alliance.guild_id == guild_id)
    ).first()
    if row is not None:
        event, alliance = row
        if administrator or get_management_rank(
            guild_id, alliance.name, actor_id, session=session,
        ) is not None:
            return event
    # Do not disclose another tenant's event or distinguish inaccessible IDs.
    raise EventManagementError(
        "❌ Event not found in this server, or you do not have permission to manage it."
    )


@dataclass(frozen=True)
class EditedEvent:
    event_id: int
    starts_at: datetime
    rescheduled: bool


def edit_event(
    sessions: sessionmaker,
    guild_id: int,
    event_id: int,
    actor_id: int,
    administrator: bool,
    *,
    name: str | None = None,
    starts_at: str | None = None,
    description: str | None = None,
    participation: str | None = None,
    audience: str | None = None,
    rsvp_deadline: str | None = None,
    missing_reminder: bool | None = None,
) -> EditedEvent:
    if all(value is None for value in (name, starts_at, description, participation, audience, rsvp_deadline, missing_reminder)):
        raise EventManagementError("❌ Provide at least one field to edit.")
    if name is not None:
        name = name.strip()
        if not name:
            raise EventManagementError("❌ Event name cannot be empty.")
    if participation is not None:
        validate_participation(participation)
    from lastz_bot.audiences import parse_audience
    new_audience = parse_audience(audience) if audience is not None else None
    new_start = None
    if starts_at is not None:
        try:
            new_start = parse_apocalypse_time(starts_at)
        except ValueError as error:
            raise EventManagementError(
                "❌ Start time must use format `YYYY-MM-DD HH:MM`."
            ) from error

    with sessions() as session:
        # Serialize with reminder claims and other edits. Never hold this lock
        # across Discord I/O. The new time and reset must commit together.
        session.execute(text("BEGIN IMMEDIATE"))
        event = _managed_event(session, guild_id, event_id, actor_id, administrator)
        rescheduled = new_start is not None and new_start != event.starts_at
        if event.series_id is not None:
            raise EventManagementError("❌ This is a weekly occurrence. Use `/event edit-series` with its series ID.")
        target_deadline = parse_deadline(rsvp_deadline) if rsvp_deadline is not None else event.rsvp_deadline
        target_reminder = missing_reminder if missing_reminder is not None else event.missing_reminder
        validate_deadline(new_start if new_start is not None else event.starts_at, target_deadline, target_reminder)
        policy_changed = (target_deadline, target_reminder) != (event.rsvp_deadline, event.missing_reminder)
        if (policy_changed or rescheduled or (participation is not None and participation != event.participation)
                or (new_audience is not None and new_audience != event.audience)):
            invalidate_missing_claims(session, [event.id])
        event.rsvp_deadline, event.missing_reminder = target_deadline, target_reminder
        if new_audience is not None and new_audience != event.audience:
            if event.starts_at <= utc_now_naive() or event.status != "scheduled":
                raise EventManagementError("❌ Historical occurrence audiences cannot be changed.")
            session.add(EventAudienceChange(event_id=event.id, previous_audience=event.audience,
                new_audience=new_audience, actor_id=actor_id, changed_at=utc_now_naive()))
            event.audience = new_audience
        if rescheduled:
            validate_one_time_start(new_start, utc_now_naive())
        if name is not None:
            event.name = name
        if description is not None:
            event.description = description.strip() or None
        if participation is not None:
            event.participation = participation
        # RSVP intentions are retained, not reconfirmed, when the time changes.
        if rescheduled:
            event.starts_at = new_start
            session.execute(delete(EventReminder).where(EventReminder.event_id == event.id))
        result = EditedEvent(event.id, event.starts_at, rescheduled)
        session.commit()
        return result


def delete_event(
    sessions: sessionmaker,
    guild_id: int,
    event_id: int,
    actor_id: int,
    administrator: bool,
) -> None:
    with sessions() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        event = _managed_event(session, guild_id, event_id, actor_id, administrator)
        if event.series_id is not None:
            raise EventManagementError("❌ This is a weekly occurrence. Use `/event stop-series` with its series ID.")
        # Delete atomically using ON DELETE CASCADE. SessionLocal enables FKs.
        session.execute(delete(Event).where(
            Event.id == event.id, Event.alliance_id == event.alliance_id,
        ))
        session.commit()
