from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint, func
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


class Member(Base):
    __tablename__ = "members"
    __table_args__ = (
        UniqueConstraint(
            "alliance_id",
            "game_name",
            name="uq_members_alliance_id_game_name",
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
