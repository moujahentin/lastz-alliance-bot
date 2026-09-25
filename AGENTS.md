# AGENTS.md — Last Z Alliance Assistant

This file is the working brief for coding agents (including Codex) contributing to this repository.

## Product goal

Last Z Alliance Assistant is a multi-tenant Discord bot for **Last Z: Survival Shooter** communities. One deployed bot should be able to serve multiple Discord guilds and multiple alliances while keeping tenant data isolated.

The bot is not only a roster bot. The intended product grows into an alliance operations assistant covering member administration, events/reminders, Alliance Duel support, announcements, personal reminders, game knowledge/guides, OCR-assisted check-ins, progression/analytics, Canyon/war planning, gift codes, alliance-specific knowledge, AI assistance, exports, and eventually a web dashboard.

## Source of truth and scope discipline

1. Existing code, database migrations, tests, and documented behavior are the source of truth for implemented behavior.
2. `README.md` describes the planned product direction, but planned features are **not** permission to invent detailed requirements.
3. Do not silently redesign working behavior or implement unrelated roadmap items while completing a focused task.
4. When a requirement is ambiguous, preserve the current architecture and choose the smallest reversible implementation. Flag product decisions that need human input.
5. Never commit secrets, tokens, `.env`, production databases, Discord credentials, or user data.

## Current implementation checkpoint

At the current `main` branch checkpoint, the project has:

- Discord slash-command client using `discord.py`.
- `/setup` guild initialization.
- Multi-guild / multi-alliance data model.
- `/alliance create` and `/alliance list`.
- Member management commands under `/member` including add/list/remove/link/rank.
- Membership-scoped ranks `R1`–`R5`, active/inactive lifecycle, and durable change history.
- Rank-aware management permissions.
- SQLAlchemy models with SQLite as the initial database.
- Alembic migrations.
- Integration/unit coverage for the permission layer.
- Event model and `/event create`, `/event list`.
- Last Z **Apocalypse Time** support for events.

Do not describe roadmap items as already implemented unless the code proves they are.

## Tenant isolation — critical invariant

`guild_id` is the top-level tenant boundary. Alliances belong to a guild; alliance-scoped records belong to an alliance.

Every read/write involving tenant-owned data must be scoped through the appropriate guild/alliance. Never look up an alliance only by name when the guild context is required. Never allow rank, Discord links, members, events, settings, strategy, assignments, reminders, or future alliance data to leak across alliances/guilds.

When adding a feature, add tests for cross-tenant isolation when applicable.

## Permissions

Current rank ordering:

- `R1` = 1, `R2` = 2, `R3` = 3
- `R4` = 4
- `R5` = 5

Current management semantics in `permissions.py`:

- `R1`–`R3` cannot manage ranks.
- Active `R4` can manage `R1`–`R4`, but not `R5`.
- Active `R5` can manage all supported ranks.
- Inactive memberships cannot manage or RSVP. Server administrators retain existing overrides.
- Self-demotion/deactivation is allowed under target-rank rules; there is no last-R5 guard.
- Member changes and audit rows commit together through `memberships.py`; never hard-delete a leaving member.
- Discord server administrators may have explicit command-level overrides where existing commands already define them.

Reuse centralized permission helpers instead of duplicating permission logic in commands. New privileged commands should follow existing patterns unless a product decision explicitly changes them.

## Event time model — critical invariant

Last Z event input is expressed in **Apocalypse Time (AT)**, currently modeled as a fixed `UTC-02:00` offset.

Current event behavior:

1. User enters `YYYY-MM-DD HH:MM` as Apocalypse Time.
2. The command attaches the fixed `UTC-02:00` offset.
3. The value is converted to UTC.
4. SQLite stores the UTC value as a naive datetime because SQLite/SQLAlchemy does not preserve timezone metadata here.
5. Comparisons against current time use UTC-naive values consistently.
6. Display converts stored UTC back to Apocalypse Time.

Example: `2026-09-25 17:00 AT` -> `2026-09-25 19:00 UTC` in storage -> displayed as `17:00 AT`.

Do not mix host-local time, Greece time, naive local `datetime.now()`, or another timezone into event scheduling. Future reminders/schedulers must use the same UTC storage invariant. If Discord timestamps are added, derive them from the canonical UTC instant so Discord can render each viewer's local time.

## Database and migrations

- SQLAlchemy ORM models live in `src/lastz_bot/database/models.py`.
- Session/database configuration lives in `src/lastz_bot/database/session.py`.
- Schema changes require Alembic migrations.
- Do not edit an already-applied migration merely to represent a new schema change; create a new migration.
- Preserve foreign keys, uniqueness constraints, rank constraints, and tenant isolation.
- Prefer database constraints for invariants that must survive concurrent requests, with friendly application-level handling of `IntegrityError` where appropriate.

For schema changes, verify at minimum:

```bash
alembic upgrade head
alembic current
alembic check
```

## Tests and validation

The project currently uses Python `unittest` discovery despite the broader roadmap/README mentioning pytest.

Before considering a change complete, run:

```bash
python -m compileall -q src tests
python -m unittest discover -s tests -v
```

For migration changes, also run the Alembic checks above.

Before committing, run:

```bash
git diff --check
```

Add focused tests for new business logic, especially permissions, tenant isolation, uniqueness, time conversion, and database behavior. Avoid relying only on manual Discord testing.

## Code organization

Current layout:

- `src/lastz_bot/main.py` — Discord client and command registration.
- `src/lastz_bot/commands/` — slash-command groups.
- `src/lastz_bot/database/` — ORM base/models/session.
- `src/lastz_bot/permissions.py` — centralized rank/management helpers.
- `src/lastz_bot/config.py` — environment-backed application settings.
- `migrations/` — Alembic migrations.
- `tests/` — automated tests.

As features grow, keep Discord interaction code thin. Put reusable business logic in dedicated modules/services rather than allowing command files to become the entire application layer. Do not introduce a large framework or abstraction without a concrete need.

## Planned product direction

These are roadmap areas, not fully specified implementation tickets:

- Event scheduling and reminders.
- Alliance Duel assistance.
- Alliance announcements.
- Personal DM reminders.
- Last Z information and guides.
- Screenshot OCR check-ins.
- Player progression tracking.
- Alliance analytics.
- Canyon and war planning.
- Gift-code tracking.
- Alliance-specific knowledge base.
- AI-assisted game/alliance support.
- CSV export.
- Web dashboard.

Future alliance-specific knowledge/settings should remain tenant-scoped. General Last Z game knowledge that is genuinely global can be shared globally rather than duplicated per guild.

## Development workflow for agents

For a requested task:

1. Inspect the relevant code, migrations, tests, and this file first.
2. State/understand the smallest implementation plan.
3. Implement only the requested slice.
4. Add/update tests.
5. Run compile/tests and migration checks when relevant.
6. Review the diff for accidental unrelated changes and secrets.
7. Use clear conventional commit messages (`feat:`, `fix:`, `test:`, `docs:`, `refactor:`).
8. Prefer a feature branch + pull request for substantial agent-generated changes rather than pushing large unreviewed changes directly to `main`.

## Security and privacy

- Treat Discord IDs and alliance/member records as user/community data.
- Never log or expose tokens/credentials.
- Do not place secrets in code, tests, docs, examples, commits, or error messages.
- Keep ephemeral/admin responses private when existing command behavior expects that.
- Validate user input and handle database conflicts without exposing internals.
- Design future OCR/analytics/data-retention features with privacy and deletion/lifecycle controls in mind.

## Near-term engineering priorities

Do not treat this ordering as immutable product policy, but when no narrower task is provided, prefer strengthening the foundation before broad feature expansion:

1. Add automated tests around event time conversion and event tenant/permission behavior.
2. Extract event time conversion into reusable/testable helpers before building reminders.
3. Design reminders/scheduling around canonical UTC instants and persistent state so restarts do not lose reminders.
4. Keep reminder destination/configuration (channel vs DM, lead times, recurrence) as explicit product decisions rather than guessing them.
5. Continue expanding features incrementally with migrations and tests.

## Human collaboration style

The repository owner prefers practical, incremental work with verification between steps. For interactive guidance, use small command batches and wait for output before assuming success. For autonomous Codex work, make changes in a reviewable branch/PR, report exactly what changed and what tests ran, and surface decisions that require product input rather than inventing them.
