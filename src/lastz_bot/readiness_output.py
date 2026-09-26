"""Bounded private pages without permanent component or navigation state."""
import discord

from lastz_bot.event_management import EventManagementError
from lastz_bot.event_time import utc_to_apocalypse_time

LABELS = (("going", "Going"), ("maybe", "Maybe"), ("not_going", "Not Going"), (None, "No Response"))


def _name(value):
    # DB length declarations are not limits in SQLite; handle imported long names
    # and line breaks as well as Markdown/mention syntax without pinging users.
    return discord.utils.escape_mentions(discord.utils.escape_markdown(" ".join(value.split())))[:100]


def _units(value):
    return len(value.encode("utf-16-le")) // 2


def readiness_page(result, page=1, *, only_no_response=False):
    groups = result.groups
    counts = " | ".join(f"{label}: {len(groups[key])}" for key, label in LABELS)
    header = (f"**{'No Response' if only_no_response else 'Roster'} — occurrence {result.event_id}**\n"
              f"{_name(result.event_name)} — {_name(result.alliance_name)}\n"
              f"{utc_to_apocalypse_time(result.starts_at):%Y-%m-%d %H:%M} AT | {result.status} | RSVP: {result.participation}\n"
              f"Eligible: {result.eligible} | Responded: {result.responded}/{result.eligible} ({result.percentage:.1f}%)\n"
              f"{counts}\n"
              "Current eligibility; RSVP is intention, not attendance or reconfirmation.\n")
    if result.participation != "required":
        header += "No Response describes missing records, not a requirement to respond.\n"
    if result.participation == "none":
        header += "RSVP is disabled; existing intentions are retained.\n"
    lines = []
    for key, label in LABELS:
        if only_no_response and key is not None:
            continue
        for member in groups[key]:
            link = f"<@{member.discord_user_id}> (linked)" if member.discord_user_id is not None else "unlinked"
            lines.append(f"• {label}: {_name(member.game_name)} — {link}")
    if not lines:
        lines = ["No eligible members without a response." if only_no_response else "No currently eligible members."]
    pages = [header]
    for line in lines:
        # Leave ample space for page metadata, including astral Unicode names.
        if _units(pages[-1] + line + "\n") > 1800:
            pages.append(header)
        pages[-1] += line + "\n"
    if page < 1 or page > len(pages):
        raise EventManagementError(f"Choose a page from 1 to {len(pages)}.")
    return pages[page - 1] + f"Page {page}/{len(pages)}. Use the command's page option for another page; each request refreshes the roster."
