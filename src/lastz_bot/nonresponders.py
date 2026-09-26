"""Current eligibility, not historical attendance or a fake RSVP response."""
from dataclasses import dataclass

from sqlalchemy import exists, select

from lastz_bot.audiences import RANKS
from lastz_bot.database.models import Alliance, EventRSVP, Member


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
    ranks = [rank for i, rank in enumerate(RANKS) if occurrence.audience & (1 << i)]
    rows = session.scalars(select(Member).join(Alliance).where(
        Member.alliance_id == occurrence.alliance_id, Alliance.guild_id == guild_id,
        Member.active.is_(True), Member.rank.in_(ranks),
        ~exists().where(EventRSVP.event_id == occurrence.id, EventRSVP.discord_user_id == Member.discord_user_id),
    ).order_by(Member.game_name, Member.id)).all()
    return tuple(NonResponder(row.id, row.game_name, row.discord_user_id) for row in rows)
