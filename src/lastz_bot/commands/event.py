from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from sqlalchemy import select

from lastz_bot.database.models import Alliance, Event, Guild
from lastz_bot.database.session import SessionLocal
from lastz_bot.permissions import get_management_rank


APOCALYPSE_TIMEZONE = timezone(timedelta(hours=-2))


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
    async def create(
        interaction: discord.Interaction,
        alliance: str,
        name: str,
        starts_at: str,
        description: str | None = None,
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
            apocalypse_starts_at = datetime.strptime(
                starts_at.strip(),
                "%Y-%m-%d %H:%M",
            ).replace(tzinfo=APOCALYPSE_TIMEZONE)

            event_starts_at = (
                apocalypse_starts_at
                .astimezone(timezone.utc)
                .replace(tzinfo=None)
            )
        except ValueError:
            await interaction.response.send_message(
                "❌ Start time must use format `YYYY-MM-DD HH:MM`.",
                ephemeral=True,
            )
            return

        if not interaction.user.guild_permissions.administrator:
            actor_rank = get_management_rank(
                guild_id=interaction.guild.id,
                alliance_name=alliance_name,
                discord_user_id=interaction.user.id,
            )

            if actor_rank is None:
                await interaction.response.send_message(
                    "❌ You need to be an R4, R5, or Server Administrator "
                    "of this alliance to create an event.",
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

            event = Event(
                alliance_id=alliance_record.id,
                name=event_name,
                description=event_description,
                starts_at=event_starts_at,
                created_by_discord_user_id=interaction.user.id,
            )

            session.add(event)
            session.commit()

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

            events = session.scalars(
                select(Event)
                .where(
                    Event.alliance_id == alliance_record.id,
                    Event.starts_at >= datetime.now(timezone.utc).replace(tzinfo=None),
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
            apocalypse_starts_at = (
                event.starts_at
                .replace(tzinfo=timezone.utc)
                .astimezone(APOCALYPSE_TIMEZONE)
            )

            line = (
                f"• `{apocalypse_starts_at:%Y-%m-%d %H:%M}` AT "
                f"— **{event.name}**"
            )

            if event.description:
                line += f" — {event.description}"

            event_lines.append(line)

        await interaction.response.send_message(
            f"**Upcoming events for `{alliance_name}`:**\n"
            + "\n".join(event_lines),
            ephemeral=True,
        )

    tree.add_command(event_group)
