import discord
from discord import app_commands
from sqlalchemy import select

from lastz_bot.database.models import Alliance, Guild
from lastz_bot.database.session import SessionLocal


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

    tree.add_command(alliance_group)
