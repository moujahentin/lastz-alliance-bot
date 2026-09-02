import os

import discord
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
    async def on_ready(self) -> None:
        if self.user is None:
            return

        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print(f"Connected to {len(self.guilds)} Discord server(s).")


def main() -> None:
    token = get_discord_token()

    intents = discord.Intents.default()
    client = LastZBot(intents=intents)

    client.run(token)


if __name__ == "__main__":
    main()
