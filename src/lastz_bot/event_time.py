"""Event times use fixed UTC-02:00 input/display and naive UTC storage."""

from datetime import datetime, timedelta, timezone


APOCALYPSE_TIMEZONE = timezone(timedelta(hours=-2))


def parse_apocalypse_time(value: str) -> datetime:
    """Parse command input as AT and return the naive UTC database value.

    Keep strptime's existing input semantics, including surrounding whitespace
    and ValueError for invalid dates or formats. AT has no daylight saving time.
    """
    apocalypse_time = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M").replace(
        tzinfo=APOCALYPSE_TIMEZONE,
    )
    return apocalypse_time.astimezone(timezone.utc).replace(tzinfo=None)


def utc_to_apocalypse_time(value: datetime) -> datetime:
    """Convert a naive UTC database value to aware AT for display.

    Attach UTC explicitly so conversion never depends on the host timezone.
    """
    return value.replace(tzinfo=timezone.utc).astimezone(APOCALYPSE_TIMEZONE)


def utc_now_naive() -> datetime:
    """Return current UTC in the same naive representation as stored events."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def discord_timestamp(value: datetime) -> str:
    """Interpret the canonical stored instant as UTC, never host-local time."""
    seconds = int(value.replace(tzinfo=timezone.utc).timestamp())
    return f"<t:{seconds}:F> (<t:{seconds}:R>)"
