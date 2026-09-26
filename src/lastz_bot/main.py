import discord
from discord import app_commands

from lastz_bot.commands.alliance import setup_alliance_commands
from lastz_bot.commands.event import setup_event_commands
from lastz_bot.commands.general import setup_general_commands
from lastz_bot.commands.member import setup_member_commands
from lastz_bot.commands.member_context import setup_member_context_commands
from lastz_bot.commands.setup import setup_setup_commands
from lastz_bot.config import load_settings
from lastz_bot.reminder_worker import ReminderWorker
from lastz_bot.event_cards import EventCards


class LastZBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)

        self.tree = app_commands.CommandTree(self)
        setup_alliance_commands(self.tree)
        setup_event_commands(self.tree)
        setup_general_commands(self.tree, self)
        setup_member_commands(self.tree)
        setup_member_context_commands(self.tree)
        setup_setup_commands(self.tree)
        self.reminder_worker = ReminderWorker(self)
        self.event_cards = EventCards(self)

    async def setup_hook(self) -> None:
        self.event_cards.register()
        await self.tree.sync()
        print("Slash commands synchronized.")
        self.reminder_worker.start()
        self.event_cards.start()

    async def close(self) -> None:
        await self.event_cards.close()
        await self.reminder_worker.close()
        await super().close()

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
