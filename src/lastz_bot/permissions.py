from __future__ import annotations

from sqlalchemy import select

from lastz_bot.database.models import Alliance, Member
from lastz_bot.database.session import SessionLocal


RANK_LEVELS = {
    "MEMBER": 1,
    "R4": 2,
    "R5": 3,
}


def has_minimum_rank(
    member_rank: str,
    minimum_rank: str,
) -> bool:
    return RANK_LEVELS.get(member_rank, 0) >= RANK_LEVELS.get(minimum_rank, 0)


def can_manage_target(
    actor_rank: str,
    target_rank: str,
) -> bool:
    if actor_rank == "R5":
        return True

    if actor_rank == "R4":
        return target_rank != "R5"

    return False


def get_member_rank(
    guild_id: int,
    alliance_name: str,
    discord_user_id: int,
) -> str | None:
    with SessionLocal() as session:
        alliance = session.scalar(
            select(Alliance).where(
                Alliance.guild_id == guild_id,
                Alliance.name == alliance_name,
            )
        )

        if alliance is None:
            return None

        member = session.scalar(
            select(Member).where(
                Member.alliance_id == alliance.id,
                Member.discord_user_id == discord_user_id,
            )
        )

        if member is None:
            return None

        return member.rank


def get_management_rank(
    guild_id: int,
    alliance_name: str,
    discord_user_id: int,
) -> str | None:
    member_rank = get_member_rank(
        guild_id=guild_id,
        alliance_name=alliance_name,
        discord_user_id=discord_user_id,
    )

    if member_rank is None:
        return None

    if not has_minimum_rank(member_rank, "R4"):
        return None

    return member_rank
