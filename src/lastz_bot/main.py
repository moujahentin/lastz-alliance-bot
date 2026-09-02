import os

from dotenv import load_dotenv


def get_discord_token() -> str:
    load_dotenv()

    token = os.getenv("DISCORD_TOKEN")

    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN is missing. Add it to the .env file."
        )

    return token


def main() -> None:
    token = get_discord_token()
    print("Discord token loaded successfully.")
    print(f"Token length: {len(token)} characters")


if __name__ == "__main__":
    main()
