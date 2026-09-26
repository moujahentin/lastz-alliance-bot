"""Read-only officer readiness, computed in one authorized database snapshot."""
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, select, text

from lastz_bot.database.models import Alliance, EventRSVP, EventSeries, Member
from lastz_bot.eligibility import eligible_membership
from lastz_bot.event_management import _managed_event


@dataclass(frozen=True)
class RosterMember:
    member_id: int
    game_name: str
    discord_user_id: int | None
    response: str | None


@dataclass(frozen=True)
class Readiness:
    event_id: int
    event_name: str
    alliance_id: int
    alliance_name: str
    starts_at: datetime
    participation: str
    status: str
    members: tuple[RosterMember, ...]

    @property
    def groups(self):
        return {key: tuple(member for member in self.members if member.response == key)
                for key in ("going", "maybe", "not_going", None)}

    @property
    def eligible(self):
        return len(self.members)

    @property
    def responded(self):
        return sum(member.response is not None for member in self.members)

    @property
    def no_response(self):
        return self.eligible - self.responded

    @property
    def percentage(self):
        return 100 * self.responded / self.eligible if self.eligible else 0.0


def get_readiness(sessions, guild_id, event_id, actor_id, administrator):
    with sessions() as session:
        # Authorization, policy, membership and responses belong to one snapshot.
        # No write lock, generation, cache, synthetic RSVP or delivery side effect.
        session.execute(text("BEGIN"))
        occurrence = _managed_event(session, guild_id, event_id, actor_id, administrator)
        alliance = session.get(Alliance, occurrence.alliance_id)
        rows = session.execute(select(Member, EventRSVP.response).join(Alliance).outerjoin(
            EventRSVP, and_(EventRSVP.event_id == occurrence.id,
                            EventRSVP.discord_user_id == Member.discord_user_id),
        ).where(Alliance.guild_id == guild_id,
                eligible_membership(occurrence.alliance_id, occurrence.audience))
            .order_by(Member.game_name, Member.id)).all()
        series = session.get(EventSeries, occurrence.series_id) if occurrence.series_id is not None else None
        status = "stopped" if series is not None and not series.active else occurrence.status
        return Readiness(occurrence.id, occurrence.name, alliance.id, alliance.name,
                         occurrence.starts_at, occurrence.participation, status,
                         tuple(RosterMember(member.id, member.game_name, member.discord_user_id, response)
                               for member, response in rows))
