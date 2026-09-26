"""Alliance-scoped membership lifecycle; no Discord I/O inside transactions."""
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from lastz_bot.database.models import Alliance, Member, MembershipChange, RSVPReminder
from lastz_bot.event_management import EventManagementError
from lastz_bot.event_time import utc_now_naive
from lastz_bot.permissions import RANK_LEVELS, can_manage_target, get_management_rank


def _alliance(session, guild_id, name):
    alliance = session.scalar(select(Alliance).where(Alliance.guild_id == guild_id, Alliance.name == name.strip()))
    if alliance is None:
        raise EventManagementError("❌ Alliance not found in this server.")
    return alliance


def _authorize(session, guild_id, alliance, actor_id, administrator, *ranks):
    if administrator:
        return
    actor = get_management_rank(guild_id, alliance.name, actor_id, session=session)
    if actor is None or any(not can_manage_target(actor, rank) for rank in ranks):
        raise EventManagementError("❌ Your active alliance rank does not permit this member change.")


def _audit(session, member, previous, actor_id):
    current = (member.rank, member.active, member.discord_user_id)
    if current == previous:
        return
    # A restored rank/link/state must never resurrect old delivery authority.
    session.execute(update(RSVPReminder).where(
        RSVPReminder.member_id == member.id,
        RSVPReminder.status.in_(("claimed", "attempted")),
    ).values(claim_token=None))
    session.add(MembershipChange(member_id=member.id,
        previous_rank=previous[0], previous_active=previous[1], previous_discord_user_id=previous[2],
        new_rank=current[0], new_active=current[1], new_discord_user_id=current[2],
        actor_id=actor_id, changed_at=utc_now_naive(), source="management"))


def add_member(sessions, guild_id, alliance_name, actor_id, administrator, *, game_name, rank="R1", discord_user_id=None):
    if rank not in RANK_LEVELS or not game_name.strip():
        raise EventManagementError("❌ Supply a game name and a rank from R1 to R5.")
    try:
        with sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            alliance = _alliance(session, guild_id, alliance_name)
            _authorize(session, guild_id, alliance, actor_id, administrator, rank)
            member = Member(alliance_id=alliance.id, game_name=game_name.strip(), rank=rank,
                            discord_user_id=discord_user_id, active=True)
            session.add(member)
            session.flush()
            _audit(session, member, (None, None, None), actor_id)
            result = member.id
            session.commit()
            return result
    except IntegrityError as error:
        raise EventManagementError("❌ That game name or Discord account already has a membership here. Reactivate an inactive membership instead.") from error


def change_member(sessions, guild_id, alliance_name, actor_id, administrator, *,
                  discord_user_id=None, game_name=None, rank=None, active=None, link_to=None):
    if rank is not None and rank not in RANK_LEVELS:
        raise EventManagementError("❌ Rank must be R1, R2, R3, R4, or R5.")
    if (discord_user_id is None) == (game_name is None):
        raise EventManagementError("❌ Select a member or provide an unlinked player's game name, not both.")
    try:
        with sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            alliance = _alliance(session, guild_id, alliance_name)
            query = select(Member).where(Member.alliance_id == alliance.id)
            query = query.where(Member.discord_user_id == discord_user_id) if discord_user_id is not None else query.where(Member.game_name == game_name.strip())
            member = session.scalar(query)
            if member is None:
                raise EventManagementError("❌ Membership not found in this alliance.")
            _authorize(session, guild_id, alliance, actor_id, administrator, member.rank, rank or member.rank)
            previous = (member.rank, member.active, member.discord_user_id)
            if rank is not None:
                member.rank = rank
            if active is not None:
                member.active = active
            if link_to is not None:
                member.discord_user_id = link_to
            _audit(session, member, previous, actor_id)
            changed = previous != (member.rank, member.active, member.discord_user_id)
            session.commit()
            return changed
    except IntegrityError as error:
        raise EventManagementError("❌ That Discord account already has a membership in this alliance.") from error


def list_members(sessions, guild_id, alliance_name):
    # Preserve the existing server-local, ephemeral roster visibility.
    with sessions() as session:
        session.execute(text("BEGIN"))
        alliance = _alliance(session, guild_id, alliance_name)
        return session.scalars(select(Member).where(Member.alliance_id == alliance.id).order_by(Member.game_name)).all()
