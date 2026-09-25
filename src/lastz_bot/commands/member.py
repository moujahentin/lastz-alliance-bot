from typing import Literal

import discord
from discord import app_commands

from lastz_bot.database.session import SessionLocal
from lastz_bot.event_management import EventManagementError
from lastz_bot.memberships import add_member, change_member, list_members

Rank = Literal["R1", "R2", "R3", "R4", "R5"]


def setup_member_commands(tree: app_commands.CommandTree) -> None:
    group = app_commands.Group(name="member", description="Manage alliance members.")

    def context(interaction):
        if interaction.guild is None:
            raise EventManagementError("❌ This command can only be used inside a Discord server.")
        return interaction.guild.id, interaction.user.id, interaction.user.guild_permissions.administrator

    async def change(interaction, alliance, member=None, game_name=None, **values):
        try:
            guild, actor, admin = context(interaction)
            changed = change_member(SessionLocal, guild, alliance, actor, admin,
                                    discord_user_id=member.id if member is not None else None,
                                    game_name=game_name, **values)
            message = "✅ Membership updated." if changed else "ℹ️ Membership already has those settings; no change made."
        except EventManagementError as error:
            message = str(error)
        await interaction.response.send_message(message, ephemeral=True)

    @group.command(name="add", description="Add a player to an alliance.")
    async def add(interaction: discord.Interaction, alliance: str, game_name: str,
                  rank: Rank = "R1", discord_user: discord.Member | None = None):
        try:
            guild, actor, admin = context(interaction)
            add_member(SessionLocal, guild, alliance, actor, admin, game_name=game_name,
                       rank=rank, discord_user_id=discord_user.id if discord_user else None)
            message = f"✅ Member `{game_name}` added to alliance `{alliance}` as {rank}."
        except EventManagementError as error:
            message = str(error)
        await interaction.response.send_message(message, ephemeral=True)

    @group.command(name="rank", description="Change an alliance member's rank.")
    async def rank(interaction: discord.Interaction, alliance: str, rank: Rank,
                   member: discord.Member | None = None, game_name: str | None = None):
        await change(interaction, alliance, member, game_name, rank=rank)

    @group.command(name="deactivate", description="Deactivate membership, preserving history and RSVPs.")
    async def deactivate(interaction: discord.Interaction, alliance: str,
                         member: discord.Member | None = None, game_name: str | None = None):
        await change(interaction, alliance, member, game_name, active=False)

    @group.command(name="activate", description="Reactivate the same historical membership.")
    async def activate(interaction: discord.Interaction, alliance: str,
                       member: discord.Member | None = None, game_name: str | None = None):
        await change(interaction, alliance, member, game_name, active=True)

    @group.command(name="remove", description="Deactivate a player; retain their membership history.")
    async def remove(interaction: discord.Interaction, alliance: str, game_name: str):
        # Compatibility entry point: leaving never deletes historical membership.
        await change(interaction, alliance, game_name=game_name, active=False)

    @group.command(name="link", description="Link an existing membership to a Discord member.")
    async def link(interaction: discord.Interaction, alliance: str, game_name: str, discord_user: discord.Member):
        await change(interaction, alliance, game_name=game_name, link_to=discord_user.id)

    @group.command(name="list", description="List alliance members, ranks, and active states.")
    async def roster(interaction: discord.Interaction, alliance: str):
        try:
            guild, _, _ = context(interaction)
            members = list_members(SessionLocal, guild, alliance)
        except EventManagementError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        lines = [f"**Members of {discord.utils.escape_markdown(alliance)}:**"]
        for row in members:
            user = f" — <@{row.discord_user_id}>" if row.discord_user_id is not None else " — unlinked"
            lines.append(f"• {discord.utils.escape_markdown(row.game_name)} — {row.rank} — {'active' if row.active else 'inactive'}{user}")
        if not members:
            lines.append("No members registered.")
        pages = [""]
        for line in lines:
            if len(pages[-1]) + len(line) + 1 > 1900:
                pages.append("")
            pages[-1] += ("\n" if pages[-1] else "") + line
        await interaction.response.send_message(pages[0], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        for page in pages[1:]:
            await interaction.followup.send(page, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    tree.add_command(group)
