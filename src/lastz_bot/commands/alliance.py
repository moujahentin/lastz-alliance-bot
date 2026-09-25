import discord
from discord import app_commands
from sqlalchemy import select, text

from lastz_bot.database.models import Alliance, Guild
from lastz_bot.database.session import SessionLocal
from lastz_bot.permissions import get_management_rank


def setup_alliance_commands(
    tree: app_commands.CommandTree,
) -> None:
    alliance_group = app_commands.Group(
        name="alliance",
        description="Manage alliances for this Discord server.",
    )

    @alliance_group.command(
        name="create",
        description="Create a new Last Z alliance.",
    )
    async def create(
        interaction: discord.Interaction,
        name: str,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used inside a Discord server.",
                ephemeral=True,
            )
            return

        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ You need the Administrator permission to create an alliance.",
                ephemeral=True,
            )
            return

        alliance_name = name.strip()

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

            existing_alliance = session.scalar(
                select(Alliance).where(
                    Alliance.guild_id == interaction.guild.id,
                    Alliance.name == alliance_name,
                )
            )

            if existing_alliance is not None:
                await interaction.response.send_message(
                    f"ℹ️ Alliance `{alliance_name}` already exists.",
                    ephemeral=True,
                )
                return

            alliance = Alliance(
                guild_id=interaction.guild.id,
                name=alliance_name,
            )

            session.add(alliance)
            session.commit()

        await interaction.response.send_message(
            f"✅ Alliance `{alliance_name}` has been created.",
            ephemeral=True,
        )

    @alliance_group.command(
        name="list",
        description="List the alliances registered for this Discord server.",
    )
    async def list_alliances(
        interaction: discord.Interaction,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used inside a Discord server.",
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

            alliances = session.scalars(
                select(Alliance)
                .where(Alliance.guild_id == interaction.guild.id)
                .order_by(Alliance.name)
            ).all()

        if not alliances:
            await interaction.response.send_message(
                "ℹ️ No alliances have been created for this Discord server yet.",
                ephemeral=True,
            )
            return

        alliance_lines = [
            f"• `{alliance.name}`"
            for alliance in alliances
        ]

        await interaction.response.send_message(
            "**Alliances:**\n" + "\n".join(alliance_lines),
            ephemeral=True,
        )

    @alliance_group.command(
        name="set-channel",
        description="Set the channel for alliance event reminders.",
    )
    async def set_channel(
        interaction: discord.Interaction,
        alliance: str,
        channel: discord.TextChannel,
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
                "❌ Alliance name cannot be empty.", ephemeral=True,
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
                    "of this alliance to configure event reminders.",
                    ephemeral=True,
                )
                return
        if channel.guild.id != interaction.guild.id:
            await interaction.response.send_message(
                "❌ Choose a channel in this Discord server.", ephemeral=True,
            )
            return
        bot_member = interaction.guild.me
        permissions = channel.permissions_for(bot_member) if bot_member is not None else None
        if permissions is None or not (permissions.view_channel and permissions.send_messages):
            await interaction.response.send_message(
                "❌ I need View Channel and Send Messages permissions in that channel.",
                ephemeral=True,
            )
            return
        with SessionLocal() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            # Recheck active membership after obtaining the same lock used by
            # member lifecycle/rank changes; preflight is not authorization.
            if not interaction.user.guild_permissions.administrator and get_management_rank(
                interaction.guild.id, alliance_name, interaction.user.id, session=session,
            ) is None:
                session.rollback()
                await interaction.response.send_message("❌ You no longer have alliance management access.", ephemeral=True)
                return
            if session.get(Guild, interaction.guild.id) is None:
                session.rollback()
                await interaction.response.send_message(
                    "❌ This Discord server has not been initialized yet. Run `/setup` first.",
                    ephemeral=True,
                )
                return
            record = session.scalar(select(Alliance).where(
                Alliance.guild_id == interaction.guild.id,
                Alliance.name == alliance_name,
            ))
            if record is None:
                session.rollback()
                await interaction.response.send_message(
                    f"❌ Alliance `{alliance_name}` does not exist.", ephemeral=True,
                )
                return
            record.reminder_channel_id = channel.id
            session.commit()
        await interaction.response.send_message(
            f"✅ Event reminders for alliance `{alliance_name}` will be sent to {channel.mention}.",
            ephemeral=True,
        )

    tree.add_command(alliance_group)
