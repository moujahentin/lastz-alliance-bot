import discord
from discord import app_commands

from lastz_bot.database.models import Guild
from lastz_bot.database.session import SessionLocal


def setup_setup_commands(
    tree: app_commands.CommandTree,
) -> None:
    @tree.command(
        name="setup",
        description="Initialize this Discord server for Last Z Alliance Assistant.",
    )
    async def setup(interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used inside a Discord server.",
                ephemeral=True,
            )
            return

        with SessionLocal() as session:
            guild = session.get(Guild, interaction.guild.id)

            if guild is None:
                guild = Guild(
                    id=interaction.guild.id,
                    name=interaction.guild.name,
                )

                session.add(guild)
                session.commit()

                await interaction.response.send_message(
                    "✅ This Discord server has been initialized.",
                    ephemeral=True,
                )
                return

            await interaction.response.send_message(
                "ℹ️ This Discord server is already initialized.",
                ephemeral=True,
            )
