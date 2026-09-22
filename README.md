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

### Editing and deleting events

`/event list alliance:<name>` includes each event's ID. R4/R5 members of the
event's alliance and Server Administrators can use `/event edit event_id:<id>`
or `/event delete event_id:<id>` in that alliance's server. Event IDs from other
servers or inaccessible alliances do not grant access.

Edit accepts optional `name`, `starts_at`, and `description` fields. Omitted
fields stay unchanged; provide at least one field. `starts_at` uses the same
`YYYY-MM-DD HH:MM` Apocalypse Time format as create. A whitespace-only description
clears it; a whitespace-only name is rejected. For example,
`/event edit event_id:12 starts_at:2026-09-26 17:00` changes only the start time.

An actual start-time change atomically removes that event's old reminder records
and stores the new UTC time. The worker derives fresh opportunities using the
existing latest-threshold-only and no-after-start rules. Name/description-only
edits, or re-entering the same start time, preserve reminder state. Delete uses
the database's existing cascade to remove the event and its reminders together.

Edit/delete and reminder claims use short SQLite write transactions, with no
network calls while holding the write lock. Each new reminder claim has a unique
token: the sender rechecks it before starting a send, and completion only updates
that exact claim. A stale send cannot mark a replacement reminder as sent, even
if the event is rescheduled away and back or a deleted integer ID is reused.
Changes committed before the final send check suppress stale work. A Discord
request already in flight (or a change from another process after that check)
cannot be recalled; it may still arrive, but cannot corrupt the new reminder
state. No distributed lock or database transaction is held across Discord I/O.

Migration `9fd174f83e21` adds the claim token needed to distinguish replacement
claims. Existing claimed/sent/skipped records remain terminal. Stop old bot
processes, run `alembic upgrade head`, then start the updated bot so all workers
use the token checks.

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
