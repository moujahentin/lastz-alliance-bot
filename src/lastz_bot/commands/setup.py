import discord
from discord import app_commands

from lastz_bot.interactions import private_command, respond
from lastz_bot.database.models import Guild
from lastz_bot.database.session import SessionLocal


def setup_setup_commands(
    tree: app_commands.CommandTree,
) -> None:
    @tree.command(
        name="setup",
        description="Initialize this Discord server for Last Z Alliance Assistant.",
    )
    @private_command
    async def setup(interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await respond(interaction,
                "❌ This command can only be used inside a Discord server.",
                ephemeral=True,
            )
            return

        if not interaction.user.guild_permissions.administrator:
            await respond(interaction,
                "❌ You need the Administrator permission to use this command.",
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

                await respond(interaction,
                    "✅ This Discord server has been initialized.",
                    ephemeral=True,
                )
                return

            await respond(interaction,
                "ℹ️ This Discord server is already initialized.",
                ephemeral=True,
            )
