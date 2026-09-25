"""Occurrence-scoped RSVP intentions. These are not attendance/reconfirmation."""
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import sessionmaker

from lastz_bot.database.models import Alliance, Event, EventRSVP, EventSeries
from lastz_bot.event_management import EventManagementError, _managed_event
from lastz_bot.event_time import utc_now_naive
from lastz_bot.permissions import get_member_rank


RESPONSES = ("going", "maybe", "not_going")


def set_rsvp(sessions: sessionmaker, guild_id: int, event_id: int,
             actor_id: int, response: str) -> None:
    if response not in RESPONSES:
        raise EventManagementError("❌ Response must be going, not_going, or maybe.")
    with sessions() as session:
        # Serialize authorization/window checks with edits, stops and deletion.
        session.execute(text("BEGIN IMMEDIATE"))
        row = session.execute(select(Event, Alliance).join(Alliance, Event.alliance_id == Alliance.id).where(
            Event.id == event_id, Alliance.guild_id == guild_id,
        )).first()
        if row is None or get_member_rank(guild_id, row[1].name, actor_id, session=session) is None:
            # Administrators have no membership bypass for submitting intentions.
            raise EventManagementError("❌ Event not found in this server, or you are not a linked member of its alliance.")
        occurrence, _ = row
        now = utc_now_naive()
        series = session.get(EventSeries, occurrence.series_id) if occurrence.series_id is not None else None
        if occurrence.status != "scheduled" or occurrence.starts_at <= now or (series is not None and not series.active):
            raise EventManagementError("❌ RSVP is closed for this occurrence.")
        if occurrence.participation == "none":
            raise EventManagementError("❌ RSVP is disabled for this event.")
        session.execute(insert(EventRSVP).values(
            event_id=event_id, discord_user_id=actor_id, response=response, created_at=now, updated_at=now,
        ).on_conflict_do_update(
            index_elements=["event_id", "discord_user_id"],
            set_={"response": response, "updated_at": now},
        ))
        session.commit()


@dataclass(frozen=True)
class RSVPSummary:
    event_id: int
    participation: str
    groups: dict[str, tuple[int, ...]]


def get_rsvps(sessions: sessionmaker, guild_id: int, event_id: int,
              actor_id: int, administrator: bool) -> RSVPSummary:
    with sessions() as session:
        session.execute(text("BEGIN"))  # Consistent authorization + summary snapshot.
        occurrence = _managed_event(session, guild_id, event_id, actor_id, administrator)
        groups = {response: [] for response in RESPONSES}
        for record in session.scalars(select(EventRSVP).where(
            EventRSVP.event_id == occurrence.id,
        ).order_by(EventRSVP.discord_user_id)):
            groups[record.response].append(record.discord_user_id)
        # Intentionally readable after disable, expiry, cancellation, or stop.
        return RSVPSummary(occurrence.id, occurrence.participation,
                           {key: tuple(users) for key, users in groups.items()})


def summary_pages(summary: RSVPSummary) -> list[str]:
    """Keep large alliance summaries within Discord's 2000-character limit."""
    lines = [
        f"**RSVPs for occurrence {summary.event_id}** — participation: {summary.participation}",
        "RSVP records intention, not attendance or reconfirmation after a schedule change.",
    ]
    if summary.participation == "none":
        lines.append("RSVP is disabled; previously stored responses are retained.")
    for response, label in (("going", "Going"), ("maybe", "Maybe"), ("not_going", "Not Going")):
        users = summary.groups[response]
        lines.append(f"**{label} ({len(users)})**")
        lines.extend(f"<@{user_id}>" for user_id in users)
        if not users:
            lines.append("—")
    pages = [""]
    for line in lines:
        if len(pages[-1]) + len(line) + 1 > 1900:
            pages.append("")
        pages[-1] += ("\n" if pages[-1] else "") + line
    return pages
