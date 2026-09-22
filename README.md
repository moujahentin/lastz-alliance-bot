# Last Z Alliance Assistant

A multi-alliance Discord bot for **Last Z: Survival Shooter** communities.

## Project Goal

Last Z Alliance Assistant is designed to help Discord communities organize and manage their alliances while providing useful game-related tools.

The bot is being designed from the beginning as a **multi-tenant system**, allowing multiple Discord servers and alliances to use the same bot while keeping their data isolated.

## Planned Features

- Multi-alliance / multi-server support
- Alliance member management
- R4 / R5 administration tools
- Event scheduling and reminders
- Alliance Duel assistance
- Announcements
- Personal DM reminders
- Last Z game information and guides
- Screenshot OCR check-ins
- Player progression tracking
- Alliance analytics
- Canyon and war planning tools
- Gift code tracking
- Alliance-specific knowledge base
- AI-assisted game and alliance support
- CSV data export
- Web dashboard

## Development Status

🚧 Early development — project foundation is currently being built.

### Alliance event reminders

An alliance R4/R5 or a Discord Server Administrator can configure one text
channel with `/alliance set-channel alliance:<name> channel:<channel>`.
The channel must belong to the same server and allow the bot to view it and send
messages. Configuration persists per alliance; no channel is selected by default.

Events retain their existing Apocalypse Time (UTC-02:00) input/display and
UTC-naive database storage. The bot polls persistent event/reminder state every
30 seconds after Discord is ready. Each event has two opportunities, at 30 and
10 minutes before its start. Polling can deliver slightly after a threshold;
the message shows the actual scheduled AT start time.

Only the latest applicable threshold is eligible: if the bot starts, an event
is created, or a channel is configured five minutes before start, only the
10-minute reminder can be attempted. The older opportunity is persistently
skipped. No new send is initiated at or after the event start. Discord/network
delivery latency is outside the bot's control.

Delivery claims are committed before sending. A claimed, sent, or skipped
opportunity is never automatically retried, including after restart or a channel
change. A crash or delivery failure may therefore lose a reminder; duplicate
suppression takes priority. An unavailable/deleted channel or missing send
permission at delivery time leaves the attempt claimed. No fallback channel or
DM is used. An unconfigured channel leaves the latest opportunity pending while
its window remains open. Separate bot workers cannot claim the same opportunity.

Apply `alembic upgrade head` before starting the updated bot. The migration adds
the alliance channel setting and delivery-state table; existing events require
no backfill. The scheduler derives pending work from event times and missing
delivery records, including events created before the migration.

## Technology

- Python
- discord.py
- SQLite (initial database)
- SQLAlchemy
- Alembic
- pytest
- Git / GitHub

## Architecture Principles

- Multi-tenant by design
- Strict isolation between Discord guilds
- Role-based access control
- Secure handling of secrets
- Modular architecture
- Testable components
- Database migrations
- Privacy and data lifecycle controls
- Production-ready logging and error handling

## License

License has not yet been selected.
