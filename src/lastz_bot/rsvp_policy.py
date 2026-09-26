"""Reporting deadlines never close RSVP; DM opt-in requires a deadline."""
from datetime import timedelta

from sqlalchemy import update

from lastz_bot.database.models import RSVPReminder
from lastz_bot.event_time import parse_apocalypse_time

MISSING_REMINDER_LEAD = timedelta(minutes=60)


def parse_deadline(value):
    from lastz_bot.event_management import EventManagementError
    if value.strip().lower() == 'none':
        return None
    try:
        return parse_apocalypse_time(value)
    except ValueError as error:
        raise EventManagementError('❌ RSVP deadline must be YYYY-MM-DD HH:MM in Apocalypse Time, or none to clear.') from error


def validate_deadline(starts_at, deadline, enabled):
    from lastz_bot.event_management import EventManagementError
    if deadline is not None and deadline >= starts_at:
        raise EventManagementError('❌ RSVP deadline must be strictly before the event start.')
    if enabled and deadline is None:
        raise EventManagementError('❌ Missing-RSVP reminders require a deadline. Disable reminders when clearing it.')


def validate_relative(minutes, enabled):
    from lastz_bot.event_management import EventManagementError
    if minutes is not None and (not isinstance(minutes, int) or minutes <= 0):
        raise EventManagementError('❌ Weekly deadline must be a positive number of minutes before each occurrence, or 0 to clear.')
    if enabled and minutes is None:
        raise EventManagementError('❌ Missing-RSVP reminders require a deadline. Disable reminders when clearing it.')


def relative_deadline(starts_at, minutes):
    from lastz_bot.event_management import EventManagementError
    try:
        return starts_at - timedelta(minutes=minutes) if minutes is not None else None
    except (OverflowError, ValueError) as error:
        raise EventManagementError("❌ Weekly deadline is outside the supported date range.") from error


def invalidate_missing_claims(session, event_ids):
    # Keep terminal attempt identity across every edit; never grant a new nag.
    session.execute(update(RSVPReminder).where(RSVPReminder.event_id.in_(event_ids),
        RSVPReminder.status.in_(('claimed', 'attempted'))).values(claim_token=None))
