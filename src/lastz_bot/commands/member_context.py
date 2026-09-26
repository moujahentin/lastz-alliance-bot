"""Temporary, actor-owned Discord UI over the shared membership service."""
import discord
from discord import app_commands

from lastz_bot.database.session import SessionLocal
from lastz_bot.event_management import EventManagementError
from lastz_bot.interactions import private_command, respond, delivery_failure
from lastz_bot.memberships import linked_memberships, change_member


def label(value):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(" ".join(value.split())[:100]))


def describe(row):
    return (f"{label(row.game_name)} — {label(row.alliance)} — {row.rank} — "
            f"{'active' if row.active else 'inactive'} — linked <@{row.discord_user_id}>")


class MemberPanel(discord.ui.View):
    def __init__(self, actor_id, guild_id, target_id, rows, *, selected=None, page=0):
        super().__init__(timeout=180)
        self.actor_id, self.guild_id, self.target_id = actor_id, guild_id, target_id
        self.rows, self.selected, self.page = rows, selected, page
        self.message = None
        if selected is None:
            choices = discord.ui.Select(placeholder="Choose an alliance membership", options=[
                discord.SelectOption(label=f"{row.alliance} — {row.game_name}"[:100], value=str(row.member_id))
                for row in rows[page * 25:(page + 1) * 25]
            ])
            async def choose(interaction):
                await self.choose(interaction, int(choices.values[0]))
            choices.callback = choose
            self.add_item(choices)
            for title, destination in (("Previous", page - 1), ("Next", page + 1)):
                if 0 <= destination < (len(rows) + 24) // 25:
                    button = discord.ui.Button(label=title)
                    async def navigate(interaction, destination=destination):
                        await self.navigate(interaction, destination)
                    button.callback = navigate
                    self.add_item(button)
        else:
            ranks = discord.ui.Select(placeholder="Set rank explicitly", options=[
                discord.SelectOption(label=f"R{i}", value=f"R{i}", default=selected.rank == f"R{i}")
                for i in range(1, 6)
            ])
            async def rank(interaction):
                await self.apply(interaction, rank=ranks.values[0])
            ranks.callback = rank
            self.add_item(ranks)
            active = discord.ui.Button(label="Deactivate" if selected.active else "Activate")
            async def state(interaction):
                # Explicit desired state, never toggle a freshly loaded value blindly.
                await self.apply(interaction, active=not selected.active)
            active.callback = state
            self.add_item(active)

    async def interaction_check(self, interaction):
        if (interaction.user.id != self.actor_id or interaction.guild_id != self.guild_id
                or self.is_finished()):
            await respond(interaction, "❌ This panel is unavailable or belongs to another user. Open your own management panel.")
            return False
        return True

    def current(self, interaction):
        return linked_memberships(SessionLocal, self.guild_id, self.target_id,
                                  actor_id=self.actor_id,
                                  administrator=interaction.user.guild_permissions.administrator)

    async def show(self, interaction):
        content = describe(self.selected) if self.selected else "Select the alliance membership to manage. No changes have been made."
        self.message = await respond(interaction, content, view=self, wait=True,
                                     allowed_mentions=discord.AllowedMentions.none())
        if self.message is None:
            self.stop()

    @private_command
    async def navigate(self, interaction, page):
        if not await self.interaction_check(interaction):
            return
        rows = self.current(interaction)
        if not rows or page * 25 >= len(rows):
            await respond(interaction, "❌ Memberships changed. Open a fresh management panel.")
            return
        await self.retire(interaction)
        await MemberPanel(self.actor_id, self.guild_id, self.target_id, rows, page=page).show(interaction)

    @private_command
    async def choose(self, interaction, member_id):
        if not await self.interaction_check(interaction):
            return
        rows = self.current(interaction)
        old = next((r for r in self.rows if r.member_id == member_id), None)
        row = next((r for r in rows if r.member_id == member_id), None)
        if row is None or row != old:
            await respond(interaction, "❌ Membership changed or access is no longer available. Open a fresh management panel.")
            return
        await self.retire(interaction)
        await MemberPanel(self.actor_id, self.guild_id, self.target_id, rows, selected=row).show(interaction)

    @private_command
    async def apply(self, interaction, **changes):
        if not await self.interaction_check(interaction):
            return
        row = self.selected
        if row is None:
            await respond(interaction, "❌ Select an alliance membership first.")
            return
        try:
            changed = change_member(SessionLocal, self.guild_id, row.alliance, self.actor_id,
                                    interaction.user.guild_permissions.administrator,
                                    discord_user_id=self.target_id, expected=row, **changes)
        except EventManagementError as error:
            await respond(interaction, str(error))
            return
        await self.retire(interaction)
        # A completed action closes the panel, including self-demotion/deactivation.
        # Reopening reads current authority; no post-commit read can mask success.
        await respond(interaction, "✅ Membership updated. Open a new panel for another action." if changed
                      else "ℹ️ Membership already has those settings; no change made. Open a new panel for another action.")

    async def retire(self, interaction=None):
        self.stop()
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException as error:
                if interaction is not None:
                    delivery_failure(interaction, "panel cleanup", error)

    async def on_timeout(self):
        await self.retire()


def setup_member_context_commands(tree):
    @private_command
    async def info(interaction: discord.Interaction, user: discord.User):
        if interaction.guild is None:
            await respond(interaction, "❌ Use this command inside a Discord server.")
            return
        rows = linked_memberships(SessionLocal, interaction.guild.id, user.id)
        if not rows:
            await respond(interaction, "No linked alliance membership for this Discord user in this server.")
            return
        # Same guild-local visibility as /member list, including inactive rows.
        pages = [""]
        for row in rows:
            line = describe(row)
            if len((pages[-1] + line).encode("utf-16-le")) // 2 > 1800:
                pages.append("")
            pages[-1] += line + "\n"
        for page in pages:
            await respond(interaction, page, allowed_mentions=discord.AllowedMentions.none())

    @private_command
    async def manage(interaction: discord.Interaction, user: discord.User):
        if interaction.guild is None:
            await respond(interaction, "❌ Use this command inside a Discord server.")
            return
        rows = linked_memberships(SessionLocal, interaction.guild.id, user.id,
                                  actor_id=interaction.user.id,
                                  administrator=interaction.user.guild_permissions.administrator)
        if not rows:
            await respond(interaction, "❌ No linked membership you can manage in this server.")
            return
        # Always require explicit selection, even when only one membership is manageable.
        await MemberPanel(interaction.user.id, interaction.guild.id, user.id, rows).show(interaction)

    for name, callback in (("Alliance Member Info", info), ("Alliance Member Manage", manage)):
        if tree.get_command(name, type=discord.AppCommandType.user) is None:
            tree.add_command(app_commands.ContextMenu(name=name, callback=callback))
