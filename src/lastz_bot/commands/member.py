import discord
from discord import app_commands
from sqlalchemy import select

from lastz_bot.database.models import Alliance, Guild, Member
from lastz_bot.database.session import SessionLocal
from lastz_bot.permissions import (
    can_manage_target,
    get_member_rank,
    has_minimum_rank,
)


def setup_member_commands(
    tree: app_commands.CommandTree,
) -> None:
    member_group = app_commands.Group(
        name="member",
        description="Manage alliance members.",
    )

    @member_group.command(
        name="add",
        description="Add a player to an alliance.",
    )
    async def add(
        interaction: discord.Interaction,
        alliance: str,
        game_name: str,
        rank: str = "MEMBER",
        discord_user: discord.User | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used inside a Discord server.",
                ephemeral=True,
            )
            return

        alliance_name = alliance.strip()
        player_name = game_name.strip()
        member_rank = rank.strip().upper()

        if not alliance_name:
            await interaction.response.send_message(
                "❌ Alliance name cannot be empty.",
                ephemeral=True,
            )
            return

        if not player_name:
            await interaction.response.send_message(
                "❌ Game name cannot be empty.",
                ephemeral=True,
            )
            return

        if member_rank not in {"MEMBER", "R4", "R5"}:
            await interaction.response.send_message(
                "❌ Rank must be one of: `MEMBER`, `R4`, `R5`.",
                ephemeral=True,
            )
            return

        if not interaction.user.guild_permissions.administrator:
            actor_rank = get_member_rank(
                guild_id=interaction.guild.id,
                alliance_name=alliance_name,
                discord_user_id=interaction.user.id,
            )

            if actor_rank is None or not has_minimum_rank(actor_rank, "R4"):
                await interaction.response.send_message(
                    "❌ You need to be an R4, R5, or Server Administrator "
                    "of this alliance to add a member.",
                    ephemeral=True,
                )
                return

            if not can_manage_target(actor_rank, member_rank):
                await interaction.response.send_message(
                    f"❌ Your rank `{actor_rank}` cannot create a `{member_rank}` member.",
                    ephemeral=True,
                )
                return

        with SessionLocal() as session:
            guild = session.get(Guild, interaction.guild.id)

            if guild is None:
                await interaction.response.send_message(
                    "❌ This Discord server has not been initialized yet. Run `/setup` first.",
                    ephemeral=True,
                )
                return

            alliance_record = session.scalar(
                select(Alliance).where(
                    Alliance.guild_id == interaction.guild.id,
                    Alliance.name == alliance_name,
                )
            )

            if alliance_record is None:
                await interaction.response.send_message(
                    f"❌ Alliance `{alliance_name}` does not exist.",
                    ephemeral=True,
                )
                return

            existing_member = session.scalar(
                select(Member).where(
                    Member.alliance_id == alliance_record.id,
                    Member.game_name == player_name,
                )
            )

            if existing_member is not None:
                await interaction.response.send_message(
                    f"ℹ️ Member `{player_name}` already exists in alliance `{alliance_name}`.",
                    ephemeral=True,
                )
                return

            member = Member(
                alliance_id=alliance_record.id,
                game_name=player_name,
                rank=member_rank,
                discord_user_id=discord_user.id if discord_user else None,
            )

            session.add(member)
            session.commit()

        discord_text = (
            f" and linked to {discord_user.mention}"
            if discord_user
            else ""
        )

        await interaction.response.send_message(
            f"✅ Member `{player_name}` has been added to alliance `{alliance_name}`{discord_text}.",
            ephemeral=True,
        )

    @member_group.command(
        name="list",
        description="List the members of an alliance.",
    )
    async def list_members(
        interaction: discord.Interaction,
        alliance: str,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used inside a Discord server.",
                ephemeral=True,
            )
            return

        alliance_name = alliance.strip()

        if not alliance_name:
            await interaction.response.send_message(
                "❌ Alliance name cannot be empty.",
                ephemeral=True,
            )
            return

        with SessionLocal() as session:
            guild = session.get(Guild, interaction.guild.id)

            if guild is None:
                await interaction.response.send_message(
                    "❌ This Discord server has not been initialized yet. Run `/setup` first.",
                    ephemeral=True,
                )
                return

            alliance_record = session.scalar(
                select(Alliance).where(
                    Alliance.guild_id == interaction.guild.id,
                    Alliance.name == alliance_name,
                )
            )

            if alliance_record is None:
                await interaction.response.send_message(
                    f"❌ Alliance `{alliance_name}` does not exist.",
                    ephemeral=True,
                )
                return

            members = session.scalars(
                select(Member)
                .where(Member.alliance_id == alliance_record.id)
                .order_by(Member.game_name)
            ).all()

        if not members:
            await interaction.response.send_message(
                f"ℹ️ Alliance `{alliance_name}` has no members yet.",
                ephemeral=True,
            )
            return

        member_lines = []

        for member in members:
            if member.discord_user_id is not None:
                member_lines.append(
                    f"• `{member.game_name}` — <@{member.discord_user_id}>"
                )
            else:
                member_lines.append(
                    f"• `{member.game_name}`"
                )

        await interaction.response.send_message(
            f"**Members of `{alliance_name}`:**\n"
            + "\n".join(member_lines),
            ephemeral=True,
        )

    @member_group.command(
        name="remove",
        description="Remove a player from an alliance.",
    )
    async def remove(
        interaction: discord.Interaction,
        alliance: str,
        game_name: str,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used inside a Discord server.",
                ephemeral=True,
            )
            return

        alliance_name = alliance.strip()
        player_name = game_name.strip()

        if not alliance_name:
            await interaction.response.send_message(
                "❌ Alliance name cannot be empty.",
                ephemeral=True,
            )
            return

        if not player_name:
            await interaction.response.send_message(
                "❌ Game name cannot be empty.",
                ephemeral=True,
            )
            return

        actor_rank = None

        if not interaction.user.guild_permissions.administrator:
            actor_rank = get_member_rank(
                guild_id=interaction.guild.id,
                alliance_name=alliance_name,
                discord_user_id=interaction.user.id,
            )

            if actor_rank is None or not has_minimum_rank(actor_rank, "R4"):
                await interaction.response.send_message(
                    "❌ You need to be an R4, R5, or Server Administrator "
                    "of this alliance to remove a member.",
                    ephemeral=True,
                )
                return

        with SessionLocal() as session:
            guild = session.get(Guild, interaction.guild.id)

            if guild is None:
                await interaction.response.send_message(
                    "❌ This Discord server has not been initialized yet. Run `/setup` first.",
                    ephemeral=True,
                )
                return

            alliance_record = session.scalar(
                select(Alliance).where(
                    Alliance.guild_id == interaction.guild.id,
                    Alliance.name == alliance_name,
                )
            )

            if alliance_record is None:
                await interaction.response.send_message(
                    f"❌ Alliance `{alliance_name}` does not exist.",
                    ephemeral=True,
                )
                return

            member = session.scalar(
                select(Member).where(
                    Member.alliance_id == alliance_record.id,
                    Member.game_name == player_name,
                )
            )

            if member is None:
                await interaction.response.send_message(
                    f"ℹ️ Member `{player_name}` does not exist in alliance `{alliance_name}`.",
                    ephemeral=True,
                )
                return

            if (
                not interaction.user.guild_permissions.administrator
                and actor_rank is not None
                and not can_manage_target(actor_rank, member.rank)
            ):
                await interaction.response.send_message(
                    f"❌ Your rank `{actor_rank}` cannot remove a `{member.rank}` member.",
                    ephemeral=True,
                )
                return

            session.delete(member)
            session.commit()

        await interaction.response.send_message(
            f"✅ Member `{player_name}` has been removed from alliance `{alliance_name}`.",
            ephemeral=True,
        )

    @member_group.command(
        name="link",
        description="Link an existing alliance member to a Discord user.",
    )
    async def link(
        interaction: discord.Interaction,
        alliance: str,
        game_name: str,
        discord_user: discord.User,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used inside a Discord server.",
                ephemeral=True,
            )
            return

        alliance_name = alliance.strip()
        player_name = game_name.strip()

        if not alliance_name:
            await interaction.response.send_message(
                "❌ Alliance name cannot be empty.",
                ephemeral=True,
            )
            return

        if not player_name:
            await interaction.response.send_message(
                "❌ Game name cannot be empty.",
                ephemeral=True,
            )
            return

        actor_rank = None

        if not interaction.user.guild_permissions.administrator:
            actor_rank = get_member_rank(
                guild_id=interaction.guild.id,
                alliance_name=alliance_name,
                discord_user_id=interaction.user.id,
            )

            if actor_rank is None or not has_minimum_rank(actor_rank, "R4"):
                await interaction.response.send_message(
                    "❌ You need to be an R4, R5, or Server Administrator "
                    "of this alliance to link a member.",
                    ephemeral=True,
                )
                return

        with SessionLocal() as session:
            alliance_record = session.scalar(
                select(Alliance).where(
                    Alliance.guild_id == interaction.guild.id,
                    Alliance.name == alliance_name,
                )
            )

            if alliance_record is None:
                await interaction.response.send_message(
                    f"❌ Alliance `{alliance_name}` does not exist.",
                    ephemeral=True,
                )
                return

            member = session.scalar(
                select(Member).where(
                    Member.alliance_id == alliance_record.id,
                    Member.game_name == player_name,
                )
            )

            if member is None:
                await interaction.response.send_message(
                    f"❌ Member `{player_name}` does not exist in alliance `{alliance_name}`.",
                    ephemeral=True,
                )
                return

            if (
                not interaction.user.guild_permissions.administrator
                and actor_rank is not None
                and not can_manage_target(actor_rank, member.rank)
            ):
                await interaction.response.send_message(
                    f"❌ Your rank `{actor_rank}` cannot link a `{member.rank}` member.",
                    ephemeral=True,
                )
                return

            member.discord_user_id = discord_user.id
            session.commit()

        await interaction.response.send_message(
            f"✅ Member `{player_name}` in alliance `{alliance_name}` "
            f"has been linked to {discord_user.mention}.",
            ephemeral=True,
        )

    @member_group.command(
        name="rank",
        description="Change the rank of an existing alliance member.",
    )
    async def rank(
        interaction: discord.Interaction,
        alliance: str,
        game_name: str,
        rank: str,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used inside a Discord server.",
                ephemeral=True,
            )
            return

        alliance_name = alliance.strip()
        player_name = game_name.strip()
        member_rank = rank.strip().upper()

        if not alliance_name:
            await interaction.response.send_message(
                "❌ Alliance name cannot be empty.",
                ephemeral=True,
            )
            return

        if not player_name:
            await interaction.response.send_message(
                "❌ Game name cannot be empty.",
                ephemeral=True,
            )
            return

        if member_rank not in {"MEMBER", "R4", "R5"}:
            await interaction.response.send_message(
                "❌ Rank must be one of: `MEMBER`, `R4`, `R5`.",
                ephemeral=True,
            )
            return

        actor_rank = None

        if not interaction.user.guild_permissions.administrator:
            actor_rank = get_member_rank(
                guild_id=interaction.guild.id,
                alliance_name=alliance_name,
                discord_user_id=interaction.user.id,
            )

            if actor_rank is None or not has_minimum_rank(actor_rank, "R4"):
                await interaction.response.send_message(
                    "❌ You need to be an R4, R5, or Server Administrator "
                    "of this alliance to change a member rank.",
                    ephemeral=True,
                )
                return

        with SessionLocal() as session:
            alliance_record = session.scalar(
                select(Alliance).where(
                    Alliance.guild_id == interaction.guild.id,
                    Alliance.name == alliance_name,
                )
            )

            if alliance_record is None:
                await interaction.response.send_message(
                    f"❌ Alliance `{alliance_name}` does not exist.",
                    ephemeral=True,
                )
                return

            member = session.scalar(
                select(Member).where(
                    Member.alliance_id == alliance_record.id,
                    Member.game_name == player_name,
                )
            )

            if member is None:
                await interaction.response.send_message(
                    f"❌ Member `{player_name}` does not exist in alliance `{alliance_name}`.",
                    ephemeral=True,
                )
                return

            if (
                not interaction.user.guild_permissions.administrator
                and actor_rank is not None
                and not can_manage_target(actor_rank, member.rank)
            ):
                await interaction.response.send_message(
                    f"❌ Your rank `{actor_rank}` cannot change a `{member.rank}` member.",
                    ephemeral=True,
                )
                return

            if (
                not interaction.user.guild_permissions.administrator
                and actor_rank is not None
                and not can_manage_target(actor_rank, member_rank)
            ):
                await interaction.response.send_message(
                    f"❌ Your rank `{actor_rank}` cannot assign rank `{member_rank}`.",
                    ephemeral=True,
                )
                return

            member.rank = member_rank
            session.commit()

        await interaction.response.send_message(
            f"✅ Member `{player_name}` in alliance `{alliance_name}` "
            f"now has rank `{member_rank}`.",
            ephemeral=True,
        )

    tree.add_command(member_group)
