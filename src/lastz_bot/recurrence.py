"""Weekly AT rules, durable occurrence generation, and series management.

No clock passage implies completion or attendance. Occurrences are scheduled
facts; historical rule segments keep backfill correct after edits and stops.
"""

from datetime import datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session, sessionmaker

from lastz_bot.database.models import Alliance, Event, EventReminder, EventSeries, WeeklySchedule
from lastz_bot.event_management import EventManagementError, validate_participation
from lastz_bot.event_time import parse_apocalypse_time, utc_now_naive, utc_to_apocalypse_time
from lastz_bot.audiences import parse_audience
from lastz_bot.rsvp_policy import relative_deadline, validate_relative, invalidate_missing_claims
from lastz_bot.permissions import get_management_rank


BACKFILL_BATCH_SIZE = 50
WEEK = timedelta(days=7)


def slot_utc(slot_at: datetime) -> datetime:
    """A naive AT rule slot becomes the existing canonical UTC-naive instant."""
    return parse_apocalypse_time(slot_at.strftime("%Y-%m-%d %H:%M"))


def next_weekly_slot(anchor_at: datetime, after_utc: datetime) -> datetime:
    """First AT slot strictly after now, never before the rule's first date."""
    after_at = utc_to_apocalypse_time(after_utc).replace(tzinfo=None)
    if anchor_at > after_at:
        return anchor_at
    return anchor_at + ((after_at - anchor_at) // WEEK + 1) * WEEK


def _insert_slot(session: Session, series: EventSeries, schedule: WeeklySchedule,
                 slot: datetime, now: datetime) -> None:
    session.execute(insert(Event).values(
        alliance_id=series.alliance_id, series_id=series.id, schedule_id=schedule.id,
        nominal_at=slot, starts_at=slot_utc(slot), name=schedule.name,
        description=schedule.description, status="scheduled", is_exception=False,
        participation=schedule.participation, participation_overridden=False,
        audience=schedule.audience, audience_overridden=False,
        rsvp_deadline=relative_deadline(slot_utc(slot), schedule.deadline_minutes),
        missing_reminder=schedule.missing_reminder, deadline_overridden=False,
        created_by_discord_user_id=series.created_by_discord_user_id, created_at=now,
    ).on_conflict_do_nothing(index_elements=["schedule_id", "nominal_at"]))


def _fill_schedule(session: Session, series: EventSeries, schedule: WeeklySchedule,
                   now: datetime, budget: int) -> int:
    cutoff = min(now, schedule.ends_at) if schedule.ends_at is not None else now
    used = 0
    while used < budget and slot_utc(schedule.next_slot_at) <= cutoff:
        _insert_slot(session, series, schedule, schedule.next_slot_at, now)
        schedule.next_slot_at += WEEK
        used += 1  # Bound examined slots as well as newly inserted rows.
    if series.active and schedule.ends_at is None:
        # Do not let years of history backlog delay the next live reminder.
        _insert_slot(session, series, schedule, next_weekly_slot(schedule.anchor_at, now), now)
    return used


def ensure_occurrences(sessions: sessionmaker, now: datetime | None = None, *,
                       guild_id: int | None = None, alliance_name: str | None = None) -> int:
    """At most 50 historical slots/cycle; at most one future slot/active series.

    Each schedule uses a separate short transaction (at most 51 insert attempts).
    The global scan is for the worker only. Command calls supply both tenant keys.
    Closed segments continue bounded backfill even after a series is stopped.
    """
    now = now if now is not None else utc_now_naive()
    if (guild_id is None) != (alliance_name is None):
        raise ValueError("Both guild and alliance scope are required")
    with sessions() as session:
        query = select(WeeklySchedule.id).join(EventSeries).join(Alliance)
        if guild_id is not None:
            query = query.where(Alliance.guild_id == guild_id, Alliance.name == alliance_name)
        ids = session.scalars(query.order_by(WeeklySchedule.next_slot_at, WeeklySchedule.id)).all()
    budget = BACKFILL_BATCH_SIZE
    for schedule_id in ids:
        with sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            query = select(WeeklySchedule, EventSeries).join(EventSeries).join(Alliance).where(
                WeeklySchedule.id == schedule_id,
            )
            if guild_id is not None:
                query = query.where(Alliance.guild_id == guild_id, Alliance.name == alliance_name)
            row = session.execute(query).first()
            if row is None:
                continue
            schedule, series = row
            budget -= _fill_schedule(session, series, schedule, now, budget)
            session.commit()
    return BACKFILL_BATCH_SIZE - budget


def create_weekly(session: Session, alliance: Alliance, name: str, description: str | None,
                  first_start_utc: datetime, actor_id: int, now: datetime,
                  participation: str = "none", audience: str = "Everyone", deadline_minutes: int | None = None,
                  missing_reminder: bool = False) -> EventSeries:
    """Called inside the create command's write transaction."""
    validate_participation(participation)
    audience_mask = parse_audience(audience)
    deadline_minutes = None if deadline_minutes == 0 else deadline_minutes
    validate_relative(deadline_minutes, missing_reminder)
    relative_deadline(first_start_utc, deadline_minutes)
    anchor = utc_to_apocalypse_time(first_start_utc).replace(tzinfo=None)
    series = EventSeries(alliance_id=alliance.id, name=name, description=description,
                         active=True, created_by_discord_user_id=actor_id, created_at=now,
                         participation=participation, audience=audience_mask,
                         deadline_minutes=deadline_minutes, missing_reminder=missing_reminder)
    session.add(series)
    session.flush()
    schedule = WeeklySchedule(series_id=series.id, anchor_at=anchor, next_slot_at=anchor,
                              name=name, description=description, participation=participation, audience=audience_mask,
                              deadline_minutes=deadline_minutes, missing_reminder=missing_reminder)
    session.add(schedule)
    session.flush()
    _fill_schedule(session, series, schedule, now, BACKFILL_BATCH_SIZE)
    return series


def _managed_series(session: Session, guild_id: int, series_id: int, actor_id: int,
                    administrator: bool) -> EventSeries:
    row = session.execute(select(EventSeries, Alliance).join(Alliance).where(
        EventSeries.id == series_id, Alliance.guild_id == guild_id,
    )).first()
    if row is not None:
        series, alliance = row
        if administrator or get_management_rank(guild_id, alliance.name, actor_id, session=session):
            return series
    raise EventManagementError(
        "❌ Series not found in this server, or you do not have permission to manage it."
    )


def _cancel_future(session: Session, series_id: int, now: datetime) -> None:
    # Retain IDs and attempted delivery records; invalidate live claim tokens.
    future_ids = select(Event.id).where(Event.series_id == series_id, Event.starts_at > now)
    session.execute(update(EventReminder).where(EventReminder.event_id.in_(future_ids)).values(claim_token=None))
    session.execute(update(Event).where(Event.id.in_(future_ids)).values(status="cancelled"))
    invalidate_missing_claims(session, future_ids)


def edit_series(sessions: sessionmaker, guild_id: int, series_id: int, actor_id: int,
                administrator: bool, *, name: str | None = None, description: str | None = None,
                weekday: int | None = None, time_at: str | None = None,
                now: datetime | None = None, participation: str | None = None,
                audience: str | None = None, deadline_minutes: int | None = None,
                missing_reminder: bool | None = None) -> None:
    audience_mask = parse_audience(audience) if audience is not None else None
    if participation is not None:
        validate_participation(participation)
    if all(v is None for v in (name, description, weekday, time_at, participation, audience, deadline_minutes, missing_reminder)):
        raise EventManagementError("❌ Provide at least one field to edit.")
    if name is not None and not name.strip():
        raise EventManagementError("❌ Event name cannot be empty.")
    if weekday is not None and weekday not in range(7):
        raise EventManagementError("❌ Choose a weekday from Monday to Sunday.")
    new_time = None
    if time_at is not None:
        try:
            new_time = datetime.strptime(time_at.strip(), "%H:%M").time()
        except ValueError as error:
            raise EventManagementError("❌ Time must use format `HH:MM` in Apocalypse Time.") from error
    with sessions() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        # Do not change newly historical participation after waiting for a lock.
        now = now if now is not None else utc_now_naive()
        series = _managed_series(session, guild_id, series_id, actor_id, administrator)
        if not series.active:
            raise EventManagementError("❌ This weekly series has been stopped.")
        old = session.scalar(select(WeeklySchedule).where(
            WeeklySchedule.series_id == series.id, WeeklySchedule.ends_at.is_(None),
        ))
        target_day = weekday if weekday is not None else old.anchor_at.weekday()
        target_time = new_time if new_time is not None else old.anchor_at.time()
        schedule_changed = (target_day, target_time) != (old.anchor_at.weekday(), old.anchor_at.time())
        target_name = name.strip() if name is not None else series.name
        target_description = (description.strip() or None) if description is not None else series.description
        target_participation = participation if participation is not None else series.participation
        target_audience = audience_mask if audience_mask is not None else series.audience
        target_minutes = (None if deadline_minutes == 0 else deadline_minutes) if deadline_minutes is not None else series.deadline_minutes
        target_reminder = missing_reminder if missing_reminder is not None else series.missing_reminder
        validate_relative(target_minutes, target_reminder)
        if not schedule_changed and (target_name, target_description, target_participation, target_audience, target_minutes, target_reminder) == (
            series.name, series.description, series.participation, series.audience, series.deadline_minutes, series.missing_reminder,
        ):
            return
        old.ends_at = now  # Preserve old snapshots/cursor until backfill finishes.
        series.name, series.description = target_name, target_description
        series.participation = target_participation
        series.audience = target_audience
        series.deadline_minutes, series.missing_reminder = target_minutes, target_reminder
        session.flush()
        if schedule_changed:
            today_at = utc_to_apocalypse_time(now).replace(tzinfo=None)
            anchor = datetime.combine(today_at.date(), target_time)
            anchor += timedelta(days=(target_day - anchor.weekday()) % 7)
            anchor = next_weekly_slot(anchor, now)
            _cancel_future(session, series.id, now)
        else:
            anchor = next_weekly_slot(old.anchor_at, now)
        new = WeeklySchedule(series_id=series.id, anchor_at=anchor, next_slot_at=anchor,
                             name=target_name, description=target_description, participation=target_participation, audience=target_audience,
                             deadline_minutes=target_minutes, missing_reminder=target_reminder)
        session.add(new)
        session.flush()
        if not schedule_changed:
            # Stable occurrence IDs and reminder claims survive metadata edits.
            # Transfer all future nominal slots, including cancelled/overridden
            # slots, so metadata changes cannot regenerate an exception.
            # Exception rows retain their independent text/time overrides.
            session.execute(update(Event).where(
                Event.series_id == series.id, Event.schedule_id == old.id,
                Event.nominal_at >= anchor,
            ).values(schedule_id=new.id))
            session.execute(update(Event).where(
                Event.schedule_id == new.id, Event.is_exception.is_(False),
                Event.starts_at > now, Event.status == "scheduled",
            ).values(name=target_name, description=target_description))
            # Existing future occurrences inherit the new mode unless explicitly
            # overridden. Past/cancelled rows and their RSVPs remain history.
            session.execute(update(Event).where(
                Event.series_id == series.id, Event.starts_at > now,
                Event.status == "scheduled", Event.participation_overridden.is_(False),
            ).values(participation=target_participation))
            invalidate_missing_claims(session, select(Event.id).where(
                Event.series_id == series.id, Event.starts_at > now,
                Event.status == "scheduled", Event.audience_overridden.is_(False),
                Event.audience != target_audience,
            ))
            session.execute(update(Event).where(
                Event.series_id == series.id, Event.starts_at > now,
                Event.status == "scheduled", Event.audience_overridden.is_(False),
            ).values(audience=target_audience))
            # Relative policies are materialized against each concrete start,
            # including time exceptions. Keep independent future overrides.
            for occurrence in session.scalars(select(Event).where(
                Event.series_id == series.id, Event.starts_at > now,
                Event.status == "scheduled", Event.deadline_overridden.is_(False),
            )):
                deadline = relative_deadline(occurrence.starts_at, target_minutes)
                if (occurrence.rsvp_deadline, occurrence.missing_reminder) != (deadline, target_reminder):
                    invalidate_missing_claims(session, [occurrence.id])
                occurrence.rsvp_deadline, occurrence.missing_reminder = deadline, target_reminder
            if target_participation != old.participation:
                invalidate_missing_claims(session, select(Event.id).where(
                    Event.series_id == series.id, Event.starts_at > now,
                    Event.status == "scheduled", Event.participation_overridden.is_(False),
                ))
        _fill_schedule(session, series, new, now, 0)
        session.commit()


def stop_series(sessions: sessionmaker, guild_id: int, series_id: int, actor_id: int,
                administrator: bool, *, now: datetime | None = None) -> None:
    now = now if now is not None else utc_now_naive()
    with sessions() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        series = _managed_series(session, guild_id, series_id, actor_id, administrator)
        if series.active:
            session.execute(update(WeeklySchedule).where(
                WeeklySchedule.series_id == series.id, WeeklySchedule.ends_at.is_(None),
            ).values(ends_at=now))
            series.active = False
            _cancel_future(session, series.id, now)
        session.commit()
