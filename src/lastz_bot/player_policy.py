"""Opt-in alliance delivery policy, independent of the two existing reminders."""
from sqlalchemy import select, text, update

from lastz_bot.database.models import Alliance, Event, PlayerReminder
from lastz_bot.event_management import EventManagementError
from lastz_bot.permissions import get_management_rank

LEADS = (1440, 60, 15)


def invalidate_player_claims(session, event_ids):
    session.execute(update(PlayerReminder).where(PlayerReminder.event_id.in_(event_ids),
        PlayerReminder.status.in_(("claimed", "attempted"))).values(claim_token=None))


def configure_delivery(sessions, guild_id, name, actor_id, administrator, *,
                       auto_publish=None, dm_24h=None, dm_1h=None, dm_15m=None):
    with sessions() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        alliance = session.scalar(select(Alliance).where(Alliance.guild_id == guild_id, Alliance.name == name.strip()))
        if alliance is None or (not administrator and get_management_rank(
                guild_id, alliance.name, actor_id, session=session) is None):
            raise EventManagementError("Alliance not found or you do not have active management access.")
        mask = alliance.player_reminder_mask
        for bit, value in enumerate((dm_24h, dm_1h, dm_15m)):
            if value is not None:
                mask = mask | (1 << bit) if value else mask & ~(1 << bit)
        if mask != alliance.player_reminder_mask:
            invalidate_player_claims(session, select(Event.id).where(Event.alliance_id == alliance.id))
            alliance.player_reminder_mask = mask
        if auto_publish is not None:
            alliance.auto_publish = auto_publish
        result = (alliance.auto_publish, mask)
        session.commit()
        return result
