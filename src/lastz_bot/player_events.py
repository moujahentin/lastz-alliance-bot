"""One tenant-scoped eligibility query for personal discovery and normal DMs."""
from datetime import timedelta, timezone

import discord
from sqlalchemy import select

from lastz_bot.database.models import Alliance, Event, EventPublication, Member
from lastz_bot.event_time import discord_timestamp, utc_to_apocalypse_time
from lastz_bot.reminders import active_occurrence
from lastz_bot.eligibility import eligible_membership


def eligible_events():
    return select(Event, Alliance, Member).join(Alliance, Event.alliance_id == Alliance.id).join(
        Member, Member.alliance_id == Alliance.id,
    ).where(eligible_membership(Event.alliance_id, Event.audience),
            Member.discord_user_id.is_not(None), active_occurrence())


def personal_events(sessions, guild_id, user_id, now, mode="mine", limit=5):
    query = eligible_events().where(Alliance.guild_id == guild_id, Member.discord_user_id == user_id,
                                    Event.starts_at > now)
    if mode == "today":
        midnight = utc_to_apocalypse_time(now).replace(hour=0, minute=0, second=0, microsecond=0)
        start = midnight.astimezone(timezone.utc).replace(tzinfo=None)
        query = query.where(Event.starts_at >= start, Event.starts_at < start + timedelta(days=1))
    if mode == "next":
        limit = 1
    with sessions() as session:
        rows = session.execute(query.order_by(Event.starts_at, Event.id).limit(limit + 1)).all()
    return rows[:limit], len(rows) > limit


def discovery_text(rows, more):
    if not rows:
        return "No upcoming events match your current active alliance membership and audience."
    lines = ["**Your eligible events:**"]
    for event, alliance, _ in rows:
        name = discord.utils.escape_markdown(event.name)[:80]
        alliance_name = discord.utils.escape_markdown(alliance.name)[:80]
        at = utc_to_apocalypse_time(event.starts_at)
        lines.append(f"• **{name}** — {alliance_name} — ID `{event.id}`\n"
                     f"  {at:%Y-%m-%d %H:%M} AT • {discord_timestamp(event.starts_at)}")
    if more:
        lines.append("More upcoming events exist; showing the earliest results.")
    return "\n".join(lines)


def card_navigation(session, occurrence, alliance):
    publication = session.scalar(select(EventPublication).where(
        EventPublication.event_id == occurrence.id, EventPublication.guild_id == alliance.guild_id,
        EventPublication.message_id.is_not(None),
    ).order_by((EventPublication.channel_id == alliance.reminder_channel_id).desc(), EventPublication.id))
    if publication is not None:
        return f"https://discord.com/channels/{publication.guild_id}/{publication.channel_id}/{publication.message_id}"
    return f"`/event rsvp event_id:{occurrence.id}`" if occurrence.participation != "none" else ""
