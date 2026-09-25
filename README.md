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

One-time creation and actual start-time changes require a start strictly later
than current UTC, checked inside the write transaction without rounding. AT
input has minute precision, so the current minute and earlier times are rejected
with an ephemeral error and no changes. Metadata-only edits and unchanged start
times remain allowed. Weekly past anchors remain valid for historical backfill.

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

### Weekly events and occurrence history

`/event create` accepts optional `recurrence:once|weekly` (default `once`). For
weekly events, `starts_at` supplies the first date, weekday, and time in AT. For
example, `starts_at:2026-09-22 17:00 recurrence:weekly` schedules Tuesdays at
17:00 AT. One-time create/edit/delete behavior stays unchanged.

`/event list` shows upcoming concrete occurrences chronologically. Weekly rows
show **Occurrence ID**, **Series ID**, and **🔁 Weekly**. Manage the template with
`/event edit-series series_id:<id>` using optional `name`, `description`,
`weekday`, and `time_at` (`HH:MM` AT). Omitted fields stay unchanged; whitespace
clears a description. `/event stop-series series_id:<id>` deactivates the series
and cancels its future occurrences, retaining their IDs and records. R4/R5
members of the alliance and administrators of its server can manage a series.
The one-time edit/delete commands reject weekly occurrence IDs and direct the
officer to the series commands. Per-occurrence editing UI is not included yet.

The existing `events` table now represents the first-class `EventOccurrence`
model (`Event` remains a compatibility alias). Each row has its own ID, concrete
UTC-naive `starts_at`, and reminder records. Migration `c41e62b79a10` preserves
existing one-time IDs, values, and delivery state in place; their series fields
remain null. New `event_series` rows hold alliance-owned templates and active
state. `weekly_schedules` holds versioned AT rules, text snapshots, and durable
backfill cursors. Rule `anchor_at`/`next_slot_at` are explicitly AT wall times;
occurrence `starts_at` and rule end cutoffs are UTC-naive instants.

Before reminder processing, each 30-second worker cycle ensures the next future
slot for every active series. It examines at most **50 historical slots total**
per cycle, including duplicates already present. Each rule's transaction has at
most 50 historical insert attempts plus one future attempt. Further cycles
continue the persisted cursor. Creation with a past first date also backfills
up to 50 slots initially. Listing performs the same bounded work for only the
requested guild/alliance. A long history backlog never delays the next live
occurrence. Unique rule/nominal-slot constraints prevent duplicate occurrences
across restarts and concurrent generation.

Backfilled rows represent **scheduled occurrences**, not proof of completion
or attendance. Time passing never changes their `scheduled` status. Historical
rows remain in place indefinitely; future attendance can reference their stable
occurrence IDs. There is no attendance inference or attendance feature here.
No reminder is sent for an expired occurrence. Each future occurrence has its
own independent 30/10-minute opportunities with the existing latest-threshold,
persisted-claim, and uncertain-delivery duplicate-suppression rules.

Metadata edits preserve future occurrence IDs/times and reminder state. A weekly
weekday/time change closes the old rule at the edit time, cancels its obsolete
future occurrences, invalidates their claim tokens, and creates the next slot
under a new rule. Past occurrences and their reminder records stay unchanged.
Closed rules retain their original text and finish bounded historical backfill,
even after edits or stops. Cancelled future rows are retained as scheduled-plan
history and are excluded from upcoming lists and reminder eligibility.

An occurrence's `nominal_at` identifies its original AT slot independently of
its actual `starts_at`. Together with `is_exception` and the reserved
scheduled/completed/cancelled status values, this permits future single-slot
reschedules and cancellations without losing the original identity or changing
later weeks. Versioned rules support a future “this and future” operation.
Metadata edits retain existing exception overrides. Full exception management
and attendance UI remain future work.

Generation, series changes/stops, and reminder claims serialize through short
SQLite `BEGIN IMMEDIATE` transactions. Composite foreign keys enforce matching
alliance/series/rule ownership; a partial unique index allows only one open rule
per series. The sender's final token/status check rejects obsolete work, and an
old completion cannot update a new occurrence's reminder. As with one-time
edits, a Discord request already in flight cannot be recalled. No database lock
is held across Discord I/O.

Stop old bot processes and run `alembic upgrade head` before deploying this
version. Downgrade to the preceding schema preserves one-time data when no
weekly series exist; it deliberately refuses to discard any weekly series or
history. Back up the database before migration.

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
