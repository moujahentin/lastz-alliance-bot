"""Shared current membership/audience predicate; callers supply guild scope.

Link and occurrence lifecycle requirements depend on the operation: reporting
includes unlinked members and closed occurrences; personal delivery does not.
"""
from sqlalchemy import and_, case

from lastz_bot.audiences import RANKS
from lastz_bot.database.models import Member


def eligible_membership(alliance_id, audience):
    rank_bit = case(*[(Member.rank == rank, 1 << i) for i, rank in enumerate(RANKS)], else_=0)
    return and_(Member.alliance_id == alliance_id, Member.active.is_(True),
                rank_bit.op("&")(audience) != 0)
