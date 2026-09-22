from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, func
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

    events: Mapped[list["Event"]] = relationship(
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


class Event(Base):
    __tablename__ = "events"

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
    )

    reminders: Mapped[list["EventReminder"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
    )


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

    event: Mapped["Event"] = relationship(back_populates="reminders")
