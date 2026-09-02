import discord
from discord import app_commands


def setup_general_commands(
    tree: app_commands.CommandTree,
    client: discord.Client,
) -> None:
    @tree.command(
        name="ping",
        description="Check whether the Last Z Alliance Assistant is online.",
    )
    async def ping(interaction: discord.Interaction) -> None:
        latency_ms = round(client.latency * 1000)

        await interaction.response.send_message(
            f"🏓 Pong! `{latency_ms} ms`"
        )
