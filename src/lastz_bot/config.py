import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    discord_token: str
    discord_application_id: str
    discord_public_key: str


def load_settings() -> Settings:
    load_dotenv()

    token = os.getenv("DISCORD_TOKEN")
    application_id = os.getenv("DISCORD_APPLICATION_ID")
    public_key = os.getenv("DISCORD_PUBLIC_KEY")

    missing = []

    if not token:
        missing.append("DISCORD_TOKEN")

    if not application_id:
        missing.append("DISCORD_APPLICATION_ID")

    if not public_key:
        missing.append("DISCORD_PUBLIC_KEY")

    if missing:
        raise RuntimeError(
            f"Missing environment variables: {', '.join(missing)}"
        )

    return Settings(
        discord_token=token,
        discord_application_id=application_id,
        discord_public_key=public_key,
    )
