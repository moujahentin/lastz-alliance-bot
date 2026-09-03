import discord
from discord import app_commands

from lastz_bot.commands.general import setup_general_commands
from lastz_bot.commands.setup import setup_setup_commands
from lastz_bot.config import load_settings


class LastZBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)

        self.tree = app_commands.CommandTree(self)
        setup_general_commands(self.tree, self)
        setup_setup_commands(self.tree)

    async def setup_hook(self) -> None:
        await self.tree.sync()
        print("Slash commands synchronized.")

    async def on_ready(self) -> None:
        if self.user is None:
            return

        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print(f"Connected to {len(self.guilds)} Discord server(s).")


client = LastZBot()


def main() -> None:
    settings = load_settings()
    client.run(settings.discord_token)


if __name__ == "__main__":
    main()
