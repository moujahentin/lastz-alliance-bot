"""Current eligibility, not historical attendance or a fake RSVP response."""
from dataclasses import dataclass

from sqlalchemy import exists, select

from lastz_bot.database.models import Alliance, EventRSVP, Member
from lastz_bot.eligibility import eligible_membership


@dataclass(frozen=True)
class NonResponder:
    member_id: int
    game_name: str
    discord_user_id: int | None


def nonresponders(session, occurrence, guild_id):
    """Caller owns a consistent snapshot/write transaction and a scoped occurrence.

    Lists current eligibility even for an old occurrence; never a historical
    non-response verdict. All three persisted response types exclude a member.
    """
    if occurrence.participation != 'required':
        return ()
    rows = session.scalars(select(Member).join(Alliance).where(
        Alliance.guild_id == guild_id,
        eligible_membership(occurrence.alliance_id, occurrence.audience),
        ~exists().where(EventRSVP.event_id == occurrence.id, EventRSVP.discord_user_id == Member.discord_user_id),
    ).order_by(Member.game_name, Member.id)).all()
    return tuple(NonResponder(row.id, row.game_name, row.discord_user_id) for row in rows)
