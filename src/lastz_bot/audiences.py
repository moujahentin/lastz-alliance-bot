"""Exact alliance-rank sets. Everyone is all five bits, independent of RSVP mode."""
from lastz_bot.event_management import EventManagementError

RANKS = ("R1", "R2", "R3", "R4", "R5")
EVERYONE = 31


def parse_audience(value: str) -> int:
    value = value.strip().upper()
    if value == "EVERYONE":
        return EVERYONE
    ranks = {part.strip() for part in value.replace("+", ",").split(",")}
    if not ranks or not ranks.issubset(RANKS):
        raise EventManagementError("❌ Audience must be Everyone or exact ranks separated by commas, such as R1,R2,R4.")
    return sum(1 << RANKS.index(rank) for rank in ranks)


def audience_label(mask: int) -> str:
    return "Everyone" if mask == EVERYONE else ", ".join(rank for i, rank in enumerate(RANKS) if mask & (1 << i))


def includes_rank(mask: int, rank: str) -> bool:
    return rank in RANKS and bool(mask & (1 << RANKS.index(rank)))
