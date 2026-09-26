from typing import Literal

import discord
from discord import app_commands
from sqlalchemy import select, text

from lastz_bot.database.models import Alliance, Event, Guild, EventAudienceChange
from lastz_bot.database.session import SessionLocal
from lastz_bot.event_management import EventManagementError, delete_event, edit_event, validate_one_time_start, validate_participation
from lastz_bot.event_time import (
    parse_apocalypse_time,
    utc_now_naive,
    utc_to_apocalypse_time,
)
from lastz_bot.audiences import parse_audience, audience_label
from lastz_bot.permissions import get_management_rank
from lastz_bot.recurrence import create_weekly, edit_series, ensure_occurrences, stop_series
from lastz_bot.rsvp_policy import parse_deadline, validate_deadline
from lastz_bot.reminders import active_occurrence
from lastz_bot.rsvp import get_rsvps, set_rsvp, summary_pages


def setup_event_commands(
    tree: app_commands.CommandTree,
) -> None:
    event_group = app_commands.Group(
        name="event",
        description="Manage alliance events.",
    )

    @event_group.command(
        name="create",
        description="Create an event for an alliance.",
    )
    @app_commands.describe(
        audience="Everyone or exact ranks separated by commas, e.g. R1,R2,R4.",
        rsvp_deadline="One-time: YYYY-MM-DD HH:MM AT. Must precede the start; does not lock RSVP.",
        deadline_minutes="Weekly: minutes before each occurrence; omit for no deadline.",
        missing_reminder="Opt in to one missing-RSVP DM per eligible member, 60 minutes before deadline.",
    )
    async def create(
        interaction: discord.Interaction,
        alliance: str,
        name: str,
        starts_at: str,
        description: str | None = None,
        recurrence: Literal["once", "weekly"] = "once",
        participation: Literal["none", "optional", "required"] = "none",
        audience: str = "Everyone",
        rsvp_deadline: str | None = None,
        deadline_minutes: app_commands.Range[int, 0] | None = None,
        missing_reminder: bool = False,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used inside a Discord server.",
                ephemeral=True,
            )
            return

        alliance_name = alliance.strip()
        event_name = name.strip()
        event_description = description.strip() if description else None

        if not alliance_name:
            await interaction.response.send_message(
                "❌ Alliance name cannot be empty.",
                ephemeral=True,
            )
            return

        if not event_name:
            await interaction.response.send_message(
                "❌ Event name cannot be empty.",
                ephemeral=True,
            )
            return

        try:
            event_starts_at = parse_apocalypse_time(starts_at)
        except ValueError:
            await interaction.response.send_message(
                "❌ Start time must use format `YYYY-MM-DD HH:MM`.",
                ephemeral=True,
            )
            return

        try:
            with SessionLocal() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                if not interaction.user.guild_permissions.administrator:
                    actor_rank = get_management_rank(
                        guild_id=interaction.guild.id,
                        alliance_name=alliance_name,
                        discord_user_id=interaction.user.id,
                        session=session,
                    )
                    if actor_rank is None:
                        raise EventManagementError(
                            "❌ You need to be an R4, R5, or Server Administrator "
                            "of this alliance to create an event."
                        )
                if session.get(Guild, interaction.guild.id) is None:
                    raise EventManagementError(
                        "❌ This Discord server has not been initialized yet. Run `/setup` first."
                    )
                alliance_record = session.scalar(select(Alliance).where(
                    Alliance.guild_id == interaction.guild.id,
                    Alliance.name == alliance_name,
                ))
                if alliance_record is None:
                    raise EventManagementError(f"❌ Alliance `{alliance_name}` does not exist.")
                validate_participation(participation)
                audience_mask = parse_audience(audience)
                if recurrence == "weekly":
                    if rsvp_deadline is not None:
                        raise EventManagementError("❌ Weekly events use deadline_minutes before each occurrence, not an absolute deadline.")
                    series = create_weekly(
                        session, alliance_record, event_name, event_description,
                        event_starts_at, interaction.user.id, utc_now_naive(), participation=participation, audience=audience,
                        deadline_minutes=deadline_minutes, missing_reminder=missing_reminder,
                    )
                    series_id = series.id
                else:
                    if deadline_minutes is not None:
                        raise EventManagementError("❌ One-time events use rsvp_deadline in Apocalypse Time.")
                    deadline = parse_deadline(rsvp_deadline) if rsvp_deadline is not None else None
                    validate_deadline(event_starts_at, deadline, missing_reminder)
                    validate_one_time_start(event_starts_at, utc_now_naive())
                    occurrence = Event(
                        alliance_id=alliance_record.id, name=event_name, description=event_description,
                        starts_at=event_starts_at, created_by_discord_user_id=interaction.user.id,
                        participation=participation, audience=audience_mask,
                        rsvp_deadline=deadline, missing_reminder=missing_reminder,
                    )
                    session.add(occurrence)
                    session.flush()
                    session.add(EventAudienceChange(event_id=occurrence.id, previous_audience=None,
                        new_audience=audience_mask, actor_id=interaction.user.id, changed_at=utc_now_naive()))
                session.commit()
        except EventManagementError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return

        apocalypse_starts_at = utc_to_apocalypse_time(event_starts_at)
        if recurrence == "weekly":
            await interaction.response.send_message(
                f"✅ Weekly series `{series_id}` (`{event_name}`) created for alliance `{alliance_name}` "
                f"from `{apocalypse_starts_at:%Y-%m-%d %H:%M}` Apocalypse Time.", ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"✅ Event `{event_name}` created for alliance `{alliance_name}` "
            f"at `{apocalypse_starts_at:%Y-%m-%d %H:%M}` Apocalypse Time.",
            ephemeral=True,
        )

    @event_group.command(
        name="list",
        description="List upcoming events for an alliance.",
    )
    async def list_events(
        interaction: discord.Interaction,
        alliance: str,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used inside a Discord server.",
                ephemeral=True,
            )
            return

        alliance_name = alliance.strip()

        if not alliance_name:
            await interaction.response.send_message(
                "❌ Alliance name cannot be empty.",
                ephemeral=True,
            )
            return

        with SessionLocal() as session:
            guild = session.get(Guild, interaction.guild.id)

            if guild is None:
                await interaction.response.send_message(
                    "❌ This Discord server has not been initialized yet. Run `/setup` first.",
                    ephemeral=True,
                )
                return

            alliance_record = session.scalar(
                select(Alliance).where(
                    Alliance.guild_id == interaction.guild.id,
                    Alliance.name == alliance_name,
                )
            )

            if alliance_record is None:
                await interaction.response.send_message(
                    f"❌ Alliance `{alliance_name}` does not exist.",
                    ephemeral=True,
                )
                return

            # Generate only this guild/alliance's next concrete occurrence.
            # End the read transaction before the generator takes a write lock.
            alliance_id = alliance_record.id
            session.rollback()
            ensure_occurrences(SessionLocal, utc_now_naive(),
                               guild_id=interaction.guild.id, alliance_name=alliance_name)
            events = session.scalars(
                select(Event)
                .join(Alliance, Event.alliance_id == Alliance.id)
                .where(
                    Event.alliance_id == alliance_id,
                    Alliance.guild_id == interaction.guild.id,
                    Alliance.name == alliance_name,
                    active_occurrence(),
                    Event.starts_at >= utc_now_naive(),
                )
                .order_by(Event.starts_at)
            ).all()

        if not events:
            await interaction.response.send_message(
                f"ℹ️ No upcoming events for alliance `{alliance_name}`.",
                ephemeral=True,
            )
            return

        event_lines = []

        for event in events:
            apocalypse_starts_at = utc_to_apocalypse_time(event.starts_at)

            line = (
                f"• ID `{event.id}` — `{apocalypse_starts_at:%Y-%m-%d %H:%M}` AT "
                f"— **{event.name}**"
            )

            if event.description:
                line += f" — {event.description}"

            if event.series_id is not None:
                line = line.replace("• ID", "• Occurrence ID", 1)
                line += f" — 🔁 Weekly (Series ID `{event.series_id}`)"

            if event.participation != "none":
                line += f" — RSVP: {event.participation}"

            if event.audience != 31:
                line += f" — Audience: {audience_label(event.audience)}"
            if event.rsvp_deadline is not None:
                deadline = utc_to_apocalypse_time(event.rsvp_deadline)
                line += f" — RSVP deadline: {deadline:%Y-%m-%d %H:%M} AT"
            event_lines.append(line)

        await interaction.response.send_message(
            f"**Upcoming events for `{alliance_name}`:**\n"
            + "\n".join(event_lines),
            ephemeral=True,
        )

    @event_group.command(name="edit", description="Edit an alliance event by ID.")
    @app_commands.describe(
        event_id="Event ID shown by /event list.",
        name="New name; omit to keep the current name.",
        starts_at="New Apocalypse Time (YYYY-MM-DD HH:MM); omit to keep it.",
        description="New description; omit to keep it, or use a space to clear it.",
        audience="Everyone or exact ranks, e.g. R3,R4,R5; omit to keep it.",
        rsvp_deadline="YYYY-MM-DD HH:MM AT, or none to clear; omit to keep it.",
        missing_reminder="Enable/disable missing-RSVP DMs; disable when clearing the deadline.",
    )
    async def edit(
        interaction: discord.Interaction,
        event_id: app_commands.Range[int, 1],
        name: str | None = None,
        starts_at: str | None = None,
        description: str | None = None,
        participation: Literal["none", "optional", "required"] | None = None,
        audience: str | None = None,
        rsvp_deadline: str | None = None,
        missing_reminder: bool | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used inside a Discord server.",
                ephemeral=True,
            )
            return
        try:
            result = edit_event(
                SessionLocal, interaction.guild.id, event_id, interaction.user.id,
                interaction.user.guild_permissions.administrator,
                name=name, starts_at=starts_at, description=description, participation=participation, audience=audience,
                rsvp_deadline=rsvp_deadline, missing_reminder=missing_reminder,
            )
        except EventManagementError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        apocalypse_starts_at = utc_to_apocalypse_time(result.starts_at)
        await interaction.response.send_message(
            f"✅ Event `{event_id}` updated. "
            f"Starts at `{apocalypse_starts_at:%Y-%m-%d %H:%M}` Apocalypse Time.",
            ephemeral=True,
        )

    @event_group.command(name="delete", description="Delete an alliance event by ID.")
    @app_commands.describe(event_id="Event ID shown by /event list.")
    async def delete(
        interaction: discord.Interaction,
        event_id: app_commands.Range[int, 1],
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used inside a Discord server.",
                ephemeral=True,
            )
            return
        try:
            delete_event(
                SessionLocal, interaction.guild.id, event_id, interaction.user.id,
                interaction.user.guild_permissions.administrator,
            )
        except EventManagementError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.send_message(f"✅ Event `{event_id}` deleted.", ephemeral=True)

    @event_group.command(name="edit-series", description="Edit a whole weekly event series.")
    @app_commands.describe(
        time_at="Weekly Apocalypse Time, HH:MM.",
        audience="Everyone or exact ranks, e.g. R3,R4,R5; omit to keep it.",
        deadline_minutes="Minutes before each occurrence, 0 to clear; omit to keep it.",
        missing_reminder="Enable/disable missing-RSVP DMs; disable when clearing the deadline.",
    )
    async def edit_weekly(
        interaction: discord.Interaction,
        series_id: app_commands.Range[int, 1],
        name: str | None = None,
        description: str | None = None,
        weekday: Literal["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"] | None = None,
        time_at: str | None = None,
        participation: Literal["none", "optional", "required"] | None = None,
        audience: str | None = None,
        deadline_minutes: app_commands.Range[int, 0] | None = None,
        missing_reminder: bool | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used inside a Discord server.", ephemeral=True,
            )
            return
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        try:
            edit_series(SessionLocal, interaction.guild.id, series_id, interaction.user.id,
                        interaction.user.guild_permissions.administrator, name=name, description=description,
                        weekday=days.index(weekday) if weekday is not None else None, time_at=time_at,
                        participation=participation, audience=audience,
                        deadline_minutes=deadline_minutes, missing_reminder=missing_reminder)
        except EventManagementError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.send_message(f"✅ Weekly series `{series_id}` updated.", ephemeral=True)

    @event_group.command(name="stop-series", description="Stop a weekly series while preserving history.")
    async def stop_weekly(interaction: discord.Interaction, series_id: app_commands.Range[int, 1]) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used inside a Discord server.", ephemeral=True,
            )
            return
        try:
            stop_series(SessionLocal, interaction.guild.id, series_id, interaction.user.id,
                        interaction.user.guild_permissions.administrator)
        except EventManagementError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ Weekly series `{series_id}` stopped. Occurrence history is preserved.", ephemeral=True,
        )

    @event_group.command(name="rsvp", description="Set your RSVP for a concrete event occurrence.")
    async def rsvp(
        interaction: discord.Interaction,
        event_id: app_commands.Range[int, 1],
        response: Literal["going", "not_going", "maybe"],
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used inside a Discord server.", ephemeral=True,
            )
            return
        try:
            set_rsvp(SessionLocal, interaction.guild.id, event_id, interaction.user.id, response)
        except EventManagementError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ RSVP for occurrence `{event_id}` set to `{response}`.", ephemeral=True,
        )
        cards = getattr(getattr(interaction, "client", None), "event_cards", None)
        if cards is not None:
            await cards.refresh(event_id=event_id)

    @event_group.command(name="rsvps", description="View an alliance occurrence's RSVP summary.")
    async def rsvps(interaction: discord.Interaction, event_id: app_commands.Range[int, 1]) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used inside a Discord server.", ephemeral=True,
            )
            return
        try:
            result = get_rsvps(SessionLocal, interaction.guild.id, event_id, interaction.user.id,
                               interaction.user.guild_permissions.administrator)
        except EventManagementError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        pages = summary_pages(result)
        await interaction.response.send_message(pages[0], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        for page in pages[1:]:
            await interaction.followup.send(page, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @event_group.command(name="publish", description="Publish a concrete alliance event card to a text channel.")
    async def publish(interaction: discord.Interaction, event_id: app_commands.Range[int, 1],
                      channel: discord.TextChannel) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used inside a Discord server.", ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            message = await interaction.client.event_cards.publish(
                interaction.guild, channel, event_id, interaction.user.id,
                interaction.user.guild_permissions.administrator,
            )
        except EventManagementError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        except discord.HTTPException:
            await interaction.followup.send("❌ Discord could not publish the event card.", ephemeral=True)
            return
        await interaction.followup.send(f"✅ Event card published: {message.jump_url}", ephemeral=True)

    async def personal(interaction, mode):
        from lastz_bot.player_events import personal_events, discovery_text
        if interaction.guild is None:
            await interaction.response.send_message("Use this command inside a Discord server.", ephemeral=True)
            return
        rows, more = personal_events(SessionLocal, interaction.guild.id, interaction.user.id, utc_now_naive(), mode)
        await interaction.response.send_message(discovery_text(rows, more), ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @event_group.command(name="mine", description="Your upcoming events matching current active membership and exact audience.")
    async def mine(interaction: discord.Interaction):
        await personal(interaction, "mine")

    @event_group.command(name="next", description="Your next eligible upcoming event.")
    async def next_event(interaction: discord.Interaction):
        await personal(interaction, "next")

    @event_group.command(name="today", description="Your upcoming events on today's Apocalypse Time calendar day.")
    async def today(interaction: discord.Interaction):
        await personal(interaction, "today")

    async def audience_suggestions(interaction: discord.Interaction, current: str):
        from lastz_bot.audiences import audience_label
        choices = [("Everyone", "Everyone")] + [(audience_label(mask), audience_label(mask).replace(" ", ""))
                                                for mask in range(1, 31)]
        needle = current.upper().replace(" ", "").replace("+", ",")
        return [app_commands.Choice(name=label, value=value) for label, value in choices
                if needle in value.upper()][:25]

    for command in (create, edit, edit_weekly):
        command.autocomplete("audience")(audience_suggestions)

    tree.add_command(event_group)
