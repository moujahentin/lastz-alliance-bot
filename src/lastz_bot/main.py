import os

import discord
from discord import app_commands
from dotenv import load_dotenv


def get_discord_token() -> str:
    load_dotenv()

    token = os.getenv("DISCORD_TOKEN")

    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN is missing. Add it to the .env file."
        )

    return token


class LastZBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)

        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        await self.tree.sync()
        print("Slash commands synchronized.")

    async def on_ready(self) -> None:
        if self.user is None:
            return

        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print(f"Connected to {len(self.guilds)} Discord server(s).")


client = LastZBot()


@client.tree.command(
    name="ping",
    description="Check whether the Last Z Alliance Assistant is online.",
)
async def ping(interaction: discord.Interaction) -> None:
    latency_ms = round(client.latency * 1000)

    await interaction.response.send_message(
        f"🏓 Pong! `{latency_ms} ms`"
    )


def main() -> None:
    token = get_discord_token()
    client.run(token)


if __name__ == "__main__":
    main()
