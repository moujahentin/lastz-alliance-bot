from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lastz_bot.database.base import Base


class Guild(Base):
    __tablename__ = "guilds"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
    )

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    alliances: Mapped[list["Alliance"]] = relationship(
        back_populates="guild",
        cascade="all, delete-orphan",
    )


class Alliance(Base):
    __tablename__ = "alliances"
    __table_args__ = (
        UniqueConstraint(
            "guild_id",
            "name",
            name="uq_alliances_guild_id_name",
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    guild_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("guilds.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    reminder_channel_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    guild: Mapped["Guild"] = relationship(
        back_populates="alliances",
    )

    members: Mapped[list["Member"]] = relationship(
        back_populates="alliance",
        cascade="all, delete-orphan",
    )

    events: Mapped[list["EventOccurrence"]] = relationship(
        back_populates="alliance",
        cascade="all, delete-orphan",
    )


class Member(Base):
    __tablename__ = "members"
    __table_args__ = (
        UniqueConstraint(
            "alliance_id",
            "game_name",
            name="uq_members_alliance_id_game_name",
        ),
        UniqueConstraint(
            "alliance_id",
            "discord_user_id",
            name="uq_members_alliance_id_discord_user_id",
        ),
        CheckConstraint(
            "rank IN ('MEMBER', 'R4', 'R5')",
            name="ck_members_rank_valid",
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    alliance_id: Mapped[int] = mapped_column(
        ForeignKey("alliances.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    game_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    rank: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="MEMBER",
        server_default="MEMBER",
    )

    discord_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    alliance: Mapped["Alliance"] = relationship(
        back_populates="members",
    )


class EventSeries(Base):
    """A weekly template; stopping it never deletes occurrence history."""

    __tablename__ = "event_series"
    __table_args__ = (
        UniqueConstraint("id", "alliance_id", name="uq_series_id_alliance"),
        CheckConstraint("participation IN ('none', 'optional', 'required')", name="ck_series_participation"),
    )

    participation: Mapped[str] = mapped_column(String(20), nullable=False, default="none", server_default="none")

    id: Mapped[int] = mapped_column(primary_key=True)
    alliance_id: Mapped[int] = mapped_column(
        ForeignKey("alliances.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    created_by_discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class WeeklySchedule(Base):
    """Versioned AT rule and backfill cursor, including closed rule segments.

    anchor_at/next_slot_at are naive AT wall times, NOT UTC storage instants.
    ends_at is an inclusive UTC cutoff; closed segments finish historical backfill.
    Name/description are snapshots for history generated after subsequent edits.
    """

    __tablename__ = "weekly_schedules"
    __table_args__ = (
        UniqueConstraint("id", "series_id", name="uq_schedule_id_series"),
        CheckConstraint("participation IN ('none', 'optional', 'required')", name="ck_schedule_participation"),
        Index("uq_weekly_schedule_open", "series_id", unique=True, sqlite_where=text("ends_at IS NULL")),
    )

    participation: Mapped[str] = mapped_column(String(20), nullable=False, default="none", server_default="none")

    id: Mapped[int] = mapped_column(primary_key=True)
    series_id: Mapped[int] = mapped_column(
        ForeignKey("event_series.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    anchor_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    next_slot_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)


class EventOccurrence(Base):
    """One concrete occurrence. Keep the legacy SQL table and one-time IDs."""

    __tablename__ = "events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["series_id", "alliance_id"], ["event_series.id", "event_series.alliance_id"],
            name="fk_occurrence_series_tenant", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["schedule_id", "series_id"], ["weekly_schedules.id", "weekly_schedules.series_id"],
            name="fk_occurrence_schedule_series", ondelete="RESTRICT",
        ),
        UniqueConstraint("schedule_id", "nominal_at", name="uq_occurrence_schedule_slot"),
        CheckConstraint(
            "(series_id IS NULL AND schedule_id IS NULL AND nominal_at IS NULL) OR "
            "(series_id IS NOT NULL AND schedule_id IS NOT NULL AND nominal_at IS NOT NULL)",
            name="ck_occurrence_series_slot",
        ),
        CheckConstraint("status IN ('scheduled', 'completed', 'cancelled')", name="ck_occurrence_status"),
        CheckConstraint("participation IN ('none', 'optional', 'required')", name="ck_occurrence_participation"),
    )

    participation: Mapped[str] = mapped_column(String(20), nullable=False, default="none", server_default="none")

    series_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    schedule_id: Mapped[int | None] = mapped_column(nullable=True)
    # Reserved for future occurrence-only participation commands.
    participation_overridden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    # Original AT slot identifies the occurrence even if starts_at is overridden.
    nominal_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="scheduled", server_default="scheduled")
    is_exception: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    alliance_id: Mapped[int] = mapped_column(
        ForeignKey("alliances.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    description: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )

    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    created_by_discord_user_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    alliance: Mapped["Alliance"] = relationship(
        back_populates="events",
        foreign_keys=[alliance_id],
    )

    reminders: Mapped[list["EventReminder"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
    )


# Compatibility names for the existing one-time API and SQL reminder FK.
Event = EventOccurrence


class EventReminder(Base):
    """A missing row is pending; every persisted status suppresses retries."""

    __tablename__ = "event_reminders"
    __table_args__ = (
        CheckConstraint("lead_minutes IN (30, 10)", name="ck_event_reminders_lead"),
        CheckConstraint(
            "status IN ('claimed', 'sent', 'skipped')",
            name="ck_event_reminders_status",
        ),
    )

    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), primary_key=True,
    )
    lead_minutes: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    # Distinguish a claim from a later replacement after an event reschedule.
    # Legacy terminal records and skipped opportunities may have no token.
    claim_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Snapshot of the destination at claim time, absent for skipped reminders.
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)

    event: Mapped["EventOccurrence"] = relationship(back_populates="reminders")


class EventRSVP(Base):
    """Current stated intention for a concrete occurrence, never attendance.

    Retained through participation disable/re-enable and one-time rescheduling.
    Retention does not mean the user reconfirmed a changed schedule.
    """

    __tablename__ = "event_rsvps"
    __table_args__ = (
        CheckConstraint("response IN ('going', 'not_going', 'maybe')", name="ck_event_rsvps_response"),
    )

    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    response: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
