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

### Participation and RSVP

`/event create` accepts `participation:none|optional|required`, defaulting to
`none` for both one-time and weekly events. `none` is informational/reminder-only;
`optional` allows responses; `required` means a response is expected. Required
does not enforce a response, block other actions, or punish members. Targeted
RSVP reminders require separate explicit opt-in and a deadline (see below).
Upcoming lists label optional/required modes and omit the default.
Officers can change the mode with `/event edit event_id:<id> participation:<mode>`
for one-time events or `/event edit-series series_id:<id> participation:<mode>`.
These use the existing alliance R4/R5 and server administrator permissions.

Active linked alliance members whose current rank is in the event audience can use
`/event rsvp event_id:<occurrence-id> response:going|not_going|maybe`. The ID must
identify a concrete occurrence in their alliance and current Discord server.
Being a server administrator alone does not grant membership for submitting an
RSVP. The event must be scheduled, not stopped/cancelled, strictly in the future,
and have optional/required participation. Responses cannot be created or changed
at/after start. Each user has one current response per occurrence; changes update
that row while retaining its original creation time.

`/event rsvps event_id:<occurrence-id>` provides an ephemeral, paginated summary
with counts and users under Going, Maybe, and Not Going. Only that alliance's
R4/R5 and administrators of its server can view it, including after expiry,
cancellation, stopping, or disabling participation. Displayed user mentions do
not send notifications. Disabling participation retains all responses, blocks
new/changed responses, and keeps existing responses available if re-enabled.
Removing a member blocks further RSVP changes without erasing their prior
intention; authorized managers can still see the stored record.

Participation is a snapshot on each occurrence and versioned weekly schedule.
Changing a series mode updates its existing **future scheduled occurrences**
unless their `participation_overridden` flag is set. Existing occurrence IDs,
RSVPs, and reminder state survive a mode-only change. Past/cancelled occurrences
are unchanged; unfinished historical backfill uses the closed schedule's saved
mode. Newly generated occurrences inherit their schedule version's mode. A
separate override flag supports later occurrence-only participation commands
independently of other exception fields; no override command is added here.
Changing the weekly time retains obsolete cancelled occurrences and their RSVPs;
replacement occurrences have new IDs and start without responses.

RSVP records **intention, not attendance**. Rescheduling a one-time event retains
its existing RSVP records without modifying their timestamps or responses.
**A retained RSVP must not be interpreted as reconfirmation after a schedule
change.** This foundation has no RSVP versioning, stale-response tracking,
reconfirmation state, attendance, non-responder chasing, or statistics.

Migration `d82a19f603b7`, following `c41e62b79a10`, defaults existing event, series,
and schedule rows to `none` and adds `event_rsvps`. Its composite primary key
enforces uniqueness for `(event_id, discord_user_id)`; deleting an occurrence
cascades its RSVP records. Timestamps are UTC-naive. RSVP writes use the same
short SQLite write-lock pattern as event management, serializing membership,
participation and time checks with edits/stops/deletes, without Discord I/O
inside the transaction. Apply `alembic upgrade head` before starting the new bot.
Downgrade refuses to discard stored RSVPs or non-default participation settings.

### Membership ranks, lifecycle, and event audiences

Each alliance membership has exactly one stored rank, R1 through R5, and an
active flag. Rank is not a Discord role or a global user property. One Discord
user can have different ranks in different alliances. New members default to R1.

Use `/member rank alliance:WaWF member:@User rank:R3`, `/member deactivate`,
`/member activate`, and `/member list alliance:WaWF`. Rank/lifecycle commands
also accept `game_name` instead of member selection for unlinked players or
members no longer available in Discord's selector. Supply exactly one selector.
`/member add` and `/member link` retain their game-name workflow and use Discord
member selection. `/member remove` is now a compatibility alias for deactivation;
it never erases the membership. List includes rank and active/inactive state,
retaining the existing server-local ephemeral roster visibility.

Active R4 can manage R1–R4, including peers; active R5 can manage every rank.
Both the target's current rank and requested rank are checked. R1–R3 cannot
manage. Inactive officers lose management access. Server administrators retain
the existing override in their current server and can restore access. Self-
demotion and self-deactivation are allowed by those same rules. There is no
last-R5 protection; server administrators are the recovery path. R4 demoted to
R3 loses management immediately; R3 promoted to active R4 gains it immediately.
An inactive R5 retains rank/history but no membership-based authority.

Deactivate/reactivate preserves the membership ID, Discord link, and historical
RSVP intentions. Deactivated members cannot submit/change RSVPs or belong to the
current eligible audience. Their old responses remain visible to authorized
managers; they are not future non-responders. Re-enabling membership does not
reconfirm old responses. Deactivation never implies attendance or authorizes
future reminders.

`membership_changes` records before/after rank, active state, and Discord link,
the membership, acting Discord user, and UTC-naive change time. Creation and link
changes are also recorded. No-op commands create no change row. Historical links
help identify intentions stored under an earlier Discord link; changing a link
does not transfer or rewrite RSVP records. These are durable application data,
not logs. Migration baselines have source `migration` and no human actor; they
record what was known at migration, not invented earlier rank/lifecycle history.

`/event create`, `/event edit`, and `/event edit-series` accept `audience:Everyone`
or exact comma-separated ranks such as `R1,R2,R4` (`+` separators also work).
Omit it on edit to preserve the current audience. Empty or unknown rank sets are
rejected. Everyone means all active memberships in that alliance. Audience and
participation are independent: optional permits eligible responses; required
expresses that eligible members are expected to respond. Deadlines and targeted
reminders are separate configuration; there are no penalties or attendance
inference. Cards display audience; upcoming lists
label restricted audiences. A visible card does not grant RSVP permission.

The database stores each exact rank set as a checked five-bit mask (R1 bit 0
through R5 bit 4); 31 represents Everyone. Existing events, series, and schedule
versions migrate to 31. The RSVP service checks current active membership and
current rank in the same `BEGIN IMMEDIATE` transaction as the response write.
Membership changes use the same serialization strategy. A membership change
committed before an RSVP obtains its lock must be observed; an RSVP committed
first remains historical intention after the later change. This applies to both
slash commands and persistent buttons, without any administrator RSVP bypass.

One-time audience edits retain before/after configuration, actor, and change time
in `event_audience_changes`. Effective audience changes to already-started or
cancelled one-time occurrences are rejected so history is not reinterpreted.
Weekly audiences follow immutable schedule versions: changing audience closes
an old version and creates a new one; bounded historical backfill keeps the old
version's audience. Existing future scheduled occurrences inherit the new set
unless their independent reserved `audience_overridden` flag is set. Metadata-
only changes retain occurrence IDs, reminder claims, exceptions, and RSVPs;
weekly time changes retain cancelled old occurrences with their original audience.
No occurrence-only override UI is added. Changing rank, state, audience, or
participation never deletes an RSVP. A retained response is not reconfirmation.

Migration `f14c38b925d0` follows PR #7's `e93b20a714c8`. Every legacy MEMBER maps
to R1 without inference; R4/R5 remain unchanged and all existing memberships
start active. Existing events, weekly history/cursors, reminders, RSVP records,
and publication bindings are preserved. Stop old bot processes, back up the
database, apply `alembic upgrade head`, then start this version. Downgrade is
allowed only before new membership/audience changes; it refuses to discard
new ranks, inactive state, audit actions, or restricted audience configuration.
Persistent routing, card reconciliation, tombstones, and reservation identity
remain unchanged; cards converge to new audiences from database snapshots.

### RSVP deadlines, No Response, and targeted reminders

RSVP deadlines are reporting/reminder deadlines, not locks. Eligible members can
still submit or change Going, Maybe, or Not Going after the deadline, until the
existing occurrence-start/lifecycle rules close RSVP. No reconfirmation or
attendance workflow is added. All existing RSVP records survive policy edits.

Use the existing commands (no additional command group):

- One-time create/edit: `rsvp_deadline:2026-10-02 18:00` in Apocalypse Time,
  optionally `missing_reminder:true` for an event starting later that day.
- One-time edit: `rsvp_deadline:none missing_reminder:false` clears the deadline.
- Weekly create (`recurrence:weekly`)/`edit-series`: `deadline_minutes:120` means
  two hours before every occurrence. `deadline_minutes:0 missing_reminder:false`
  clears it. Absolute deadlines are rejected for weekly create; relative options
  are rejected for one-time create.
- Omitted edit options preserve existing values. `missing_reminder:false`
  disables DMs without clearing the deadline. Lead time is fixed at 60 minutes
  in this PR; no configurable lead option or repeated reminders are added.

An absolute deadline is stored as canonical UTC-naive time using the same AT
helpers as event starts. It must be strictly before the occurrence; equality or
later is rejected by business validation and database constraints. A past
deadline on a future event is allowed because it reports a target, not a hard
lock; it produces no catch-up DM after the deadline. One-time rescheduling keeps
the absolute deadline unless explicitly edited. Rescheduling before/equal to it
is rejected atomically unless the deadline is changed/cleared in the same edit.
Clearing a deadline requires explicitly disabling an enabled reminder; settings
are never silently changed to fix invalid input. Stored reminder opt-in may
remain dormant on optional/none events; only required events can send these DMs.

For required events, `/event rsvps` shows **No Response** alongside the three
response groups. This is computed from currently active memberships in the
same guild/alliance, their current rank against the occurrence's audience, and
the absence of a stored RSVP for their current Discord link. Going, Maybe, and
Not Going all exclude a member. Unlinked eligible players appear by game name,
marked unlinked/cannot DM. No synthetic RSVP rows are written. Even when viewing
an old occurrence this is explicitly current eligibility, not a reconstruction
of historical non-response, a penalty, or evidence of attendance. Optional/none
summaries do not label members as mandatory non-responders.

Cards show a required-event No Response count and the AT deadline when present.
A passed-deadline label does not disable otherwise valid buttons. Existing
approximately 30-second reconciliation updates membership/audience/count/policy
changes across all cards; backend checks remain authoritative while cards lag.

The existing reminder worker runs a targeted RSVP processor after its normal
occurrence-generation and event-reminder work. It sends only in the interval
`deadline - 60 minutes <= now < deadline`, and only before event start. Restart
inside that window may send the one opportunity; restart at/after the deadline
cannot. Existing channel reminders and their 30/10-minute rules are unchanged.
No extra backfill pass or independent scheduler is introduced.

`rsvp_reminders` enforces one terminal opportunity per occurrence/Discord user
and per occurrence/membership. The worker commits `claimed` before lookup,
resolves the Discord user and private channel, then acquires a short SQLite
`BEGIN IMMEDIATE` transaction to recheck the claim token, tenant, current active
membership/rank/audience, required participation, current deadline/window,
series state, and missing response. It atomically changes `claimed` to
`attempted` before starting the DM. Even concurrent replay of the same delivery
cannot authorize a second send. Successful completion becomes `sent` only if
its token still matches. No database lock spans Discord I/O.

Every persisted state suppresses retry. Blocked DMs, lookup errors, uncertain
HTTP failures, cancellation, crashes, or failed success recording can therefore
lose a reminder, but never automatically produce a duplicate application send.
Failures are not recorded as successfully sent. Unlinked members never get a
claim or count as delivered. No public missing-response announcement is sent.

Effective membership rank/state/link changes, occurrence audience/participation/
deadline/opt-in changes, and reschedules invalidate affected outstanding claim
tokens without deleting their terminal records. Changing a setting away and back
cannot resurrect an old claim or grant another nag. No-op settings and text-only
edits preserve valid pending authority. Series stops invalidate future claims;
event deletion cascades claims, and unique tokens protect against SQLite event-ID
reuse or stale completion. An RSVP committed before final authorization suppresses
the DM. A Discord request already authorized/in flight cannot be recalled;
changes after that final check can still race the network send, as with existing
event reminders. This boundary is deliberate and covered by tests.

Weekly series and schedule versions store a nullable positive minute offset plus
opt-in; each occurrence materializes its own absolute UTC deadline. Metadata
policy edits preserve existing future occurrence IDs/RSVPs and inherit the new
policy unless the independent reserved `deadline_overridden` flag is set.
Historical/cancelled rows retain their configuration. Closed versions finish
bounded historical backfill with their original deadline/reminder policy, without
sending expired reminders. Weekday/time changes retain cancelled old occurrences
and create new concrete occurrences under the new policy. No per-occurrence
exception UI is added.

Migration `a25d49c036e1` follows `f14c38b925d0`. Existing events and weekly
configurations receive no deadline and disabled targeted reminders; all prior
memberships, audit data, events/history, RSVPs, reminder claims and card bindings
are preserved. Downgrade refuses to discard configured deadlines or RSVP reminder
attempts. Stop old processes, back up the database, run `alembic upgrade head`,
and restart before using the new options.

### Persistent event cards

An alliance R4/R5 or server administrator can explicitly publish a concrete
occurrence with `/event publish event_id:<id> channel:<text-channel>`. The channel
must be in the same server and grant the bot View Channel, Send Messages, and
Embed Links. Creation never publishes automatically. Cards show the event text,
AT start, alliance, occurrence/series identity, participation mode, and database
RSVP counts. Optional/required events have Going, Maybe, and Not Going buttons;
`none` displays RSVP disabled with no controls. Required is clearly labelled.
The existing `/event rsvp` and `/event rsvps` commands remain available.

Each button delegates to the same RSVP service as the slash command. Linked
membership, tenant ownership, participation and lifecycle checks still apply;
administrator status alone does not permit RSVP. The service atomically checks
the stored guild/channel/message/event association before writing. Custom IDs
contain only a version and response (`lastz:rsvp:v1:going`, `:maybe`, and
`:not_going`), never authoritative event/user/tenant identifiers.

Startup registers a global discord.py persistent view with `timeout=None` and
those stable IDs. Old messages therefore route to the new process; every click
resolves the occurrence from the database. No message needs to be republished
after restart. The worker waits for Discord readiness and reconciles cards every
30 seconds. Successful button and slash RSVPs also request an immediate refresh.
All cards for an occurrence share one RSVP dataset. Refresh sends are serialized
within the bot process and reread database snapshots; counts are never adjusted
from message text. Unchanged renders are skipped. Permission/network failures
retry on later cycles without undoing committed RSVPs. This is eventual visual
consistency, not a transaction spanning Discord and SQLite.

Mode changes update existing cards on reconciliation: none removes controls,
and re-enabling restores them when the occurrence is still open. Expiry,
cancellation and series stops close controls. Correctness never depends on the
Discord edit succeeding: stale clicks still fail backend checks. A card always
belongs to its original occurrence; it never rolls forward to another week.

Migration `e93b20a714c8` follows `d82a19f603b7` and adds `event_publications`.
Each publication has a non-reused SQLite reservation ID, a unique nullable
Discord message ID, guild/channel association, nullable occurrence FK, and
UTC-naive creation time. Multiple messages/channels per occurrence are supported.
Publishing reserves the occurrence and reads its initial display snapshot inside
one transaction, then sends an inert message. Only after persisting its message
ID are controls enabled. Authorization is checked again at registration. No
database transaction spans Discord I/O, and uncertain sends are never retried
automatically. A crash before registration can leave an inert Discord message;
it may require manual deletion. Unregistered reservations are pruned after one
hour; late completions cannot bind to a new reservation and attempt to delete
their inert message. Failed DB cleanup does not skip attempted Discord cleanup.

Deleting an occurrence sets publication FKs to null, preserving cleanup
tombstones. The worker replaces their cards with an unavailable message and
removes controls, then deletes the publication record. Discord Not Found also
removes only that publication; RSVP/event data is untouched. Missing permissions
retain tombstones for retry. Guild deletion cascades its publication records.
Binding and card data are read in one database snapshot, so event deletion and
SQLite event-ID reuse cannot retarget an old card. A Discord edit already in
flight cannot be recalled, but subsequent reconciliation corrects it and stale
buttons cannot authorize RSVP for a replacement occurrence.

Apply the migration before starting this version. Downgrade refuses while
publication records remain, to avoid orphaning active cards. No attendance,
automatic role mentions, RSVP reminders, deadlines, or statistics are added.

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
