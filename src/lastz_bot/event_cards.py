"""Persistent Discord controls and eventual multi-card synchronization."""
import asyncio
from contextlib import suppress
import logging

import discord
from discord.ext import tasks
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError

from lastz_bot.database.models import EventPublication, Event
from lastz_bot.database.session import SessionLocal
from lastz_bot.event_management import EventManagementError
from lastz_bot.event_time import utc_to_apocalypse_time, discord_timestamp, utc_now_naive
from lastz_bot.automatic_publications import (candidates, PUBLICATION_BATCH, reserve_automatic, authorize_automatic, register_automatic)
from lastz_bot.publications import (
    abandon_publication, authorize_publish, prune_pending_publications,
    read_publication_card, record_publication, reserve_publication, resolve_publication,
)
from lastz_bot.audiences import audience_label
from lastz_bot.rsvp import set_rsvp


logger = logging.getLogger(__name__)
CUSTOM_IDS = {key: f"lastz:rsvp:v1:{key}" for key in ("going", "maybe", "not_going")}


class RSVPView(discord.ui.View):
    def __init__(self, cards):
        super().__init__(timeout=None)
        for response, label, emoji, style in (
            ("going", "Going", "✅", discord.ButtonStyle.success),
            ("maybe", "Maybe", "❓", discord.ButtonStyle.secondary),
            ("not_going", "Not Going", "❌", discord.ButtonStyle.danger),
        ):
            button = discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=CUSTOM_IDS[response])
            async def callback(interaction, choice=response):
                await cards.respond(interaction, choice)
            button.callback = callback
            self.add_item(button)


def card_embed(state):
    if state is None:
        return discord.Embed(title="Event unavailable", description="This occurrence was deleted or is no longer available. RSVP is closed.")
    embed = discord.Embed(title=state.name[:256], description=(state.description or "")[:4096],
                          colour=discord.Colour.blurple())
    starts = utc_to_apocalypse_time(state.starts_at)
    embed.add_field(name="Apocalypse Time", value=f"{starts:%Y-%m-%d %H:%M} AT", inline=False)
    embed.add_field(name="Your local time", value=discord_timestamp(state.starts_at), inline=False)
    embed.add_field(name="Alliance", value=discord.utils.escape_markdown(state.alliance)[:1024])
    embed.add_field(name="Event", value=f"Weekly — Series ID {state.series_id}" if state.series_id else "One-time")
    embed.add_field(name="Audience", value=audience_label(state.audience), inline=False)
    modes = {"none": "RSVP disabled", "optional": "RSVP optional", "required": "Response required"}
    embed.add_field(name="Participation", value=modes[state.participation], inline=False)
    if state.status != "scheduled":
        embed.add_field(name="Status", value=f"{state.status.capitalize()} — RSVP closed", inline=False)
    for label, count in zip(("Going", "Maybe", "Not Going"), state.counts):
        embed.add_field(name=label, value=str(count))
    if state.no_response is not None:
        embed.add_field(name="No Response", value=str(state.no_response))
    if state.deadline is not None:
        deadline = utc_to_apocalypse_time(state.deadline)
        label = "RSVP deadline passed" if state.deadline_passed else "RSVP Deadline"
        embed.add_field(name=label, value=f"{deadline:%Y-%m-%d %H:%M} AT", inline=False)
    footer = f"Occurrence ID {state.event_id} • RSVP is intention, not attendance or reconfirmation."
    if state.no_response is not None:
        footer += " No Response uses current eligibility."
    embed.set_footer(text=footer)
    return embed


class EventCards:
    def __init__(self, client, sessions=None):
        self.client = client
        self.sessions = sessions if sessions is not None else SessionLocal
        self.lock = asyncio.Lock()
        self.rendered = {}
        self.registered = False
        # Scan bounded pages fairly even when an earlier destination stays broken.
        # This cursor is only an optimization; all delivery authority is in SQLite.
        self.automatic_cursor = 0

    def register(self):
        # A global persistent router covers old messages without recreating per-
        # occurrence in-memory objects. The DB association is checked on every press.
        if not self.registered:
            self.client.add_view(RSVPView(self))
            self.registered = True

    async def publish(self, guild, channel, event_id, actor_id, administrator):
        authorize_publish(self.sessions, guild.id, event_id, actor_id, administrator)
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
            raise EventManagementError("❌ Choose a text channel in this server.")
        permissions = channel.permissions_for(guild.me)
        if not all((permissions.view_channel, permissions.send_messages, permissions.embed_links)):
            raise EventManagementError("❌ I need View Channel, Send Messages, and Embed Links in that channel.")
        # Reserve the occurrence identity before network I/O. SET NULL catches
        # deletion even if SQLite later reuses its integer event ID.
        reservation, state = reserve_publication(self.sessions, guild.id, event_id, channel.id, actor_id, administrator)
        message = None
        try:
            # Inert until persisted. Never automatically retry an uncertain send.
            message = await channel.send(embed=card_embed(state), allowed_mentions=discord.AllowedMentions.none())
            record_publication(self.sessions, reservation, message.id, actor_id, administrator)
        except (SQLAlchemyError, EventManagementError, discord.HTTPException):
            # DB cleanup and Discord cleanup are independent: failure of either
            # must not skip the other. Pending leftovers expire after one hour.
            with suppress(SQLAlchemyError):
                abandon_publication(self.sessions, reservation)
            if message is not None:
                with suppress(discord.HTTPException):
                    await message.delete()
            raise EventManagementError("❌ Could not register the event card; please try publishing again.")
        await self.refresh(event_id=event_id)
        return message

    async def publish_automatic(self):
        try:
            with self.sessions() as session:
                query = candidates(utc_now_naive()).where(Event.id > self.automatic_cursor)
                rows = session.execute(query.order_by(Event.id).limit(PUBLICATION_BATCH)).all()
                if not rows and self.automatic_cursor:
                    rows = session.execute(candidates(utc_now_naive()).order_by(Event.id).limit(PUBLICATION_BATCH)).all()
                self.automatic_cursor = rows[-1][0].id if rows else 0
            for event, alliance in rows:
                guild = self.client.get_guild(alliance.guild_id)
                channel = guild.get_channel(alliance.reminder_channel_id) if guild else None
                if not isinstance(channel, discord.TextChannel) or channel.guild.id != alliance.guild_id:
                    continue  # No send was possible; configuration may be repaired.
                permissions = channel.permissions_for(guild.me)
                if not all((permissions.view_channel, permissions.send_messages, permissions.embed_links)):
                    continue
                reservation = reserve_automatic(self.sessions, event.id, alliance.guild_id, channel.id, utc_now_naive)
                if reservation is None:
                    continue
                message = None
                try:
                    state = authorize_automatic(self.sessions, reservation, utc_now_naive)
                    if state is None:
                        continue
                    # Final serialized authorization precedes send without an await.
                    message = await channel.send(embed=card_embed(state), allowed_mentions=discord.AllowedMentions.none())
                    if not register_automatic(self.sessions, reservation, message.id):
                        raise EventManagementError("Automatic reservation no longer exists")
                except (SQLAlchemyError, discord.HTTPException, EventManagementError):
                    with suppress(SQLAlchemyError):
                        abandon_publication(self.sessions, reservation)
                    if message is not None:
                        with suppress(discord.HTTPException):
                            await message.delete()
                    logger.warning("Automatic event card attempt failed; no automatic retry: event=%s", event.id)
        except SQLAlchemyError:
            logger.warning("Automatic publication database processing failed; will poll again")

    async def respond(self, interaction, response):
        await interaction.response.defer(ephemeral=True)
        try:
            if (interaction.guild is None or interaction.message is None or self.client.user is None
                    or interaction.message.author.id != self.client.user.id):
                raise EventManagementError("❌ This event card is no longer available.")
            event_id = resolve_publication(self.sessions, interaction.guild.id,
                                           interaction.channel_id, interaction.message.id)
            # Revalidate association atomically with the existing RSVP business rules.
            set_rsvp(self.sessions, interaction.guild.id, event_id, interaction.user.id, response,
                     publication_channel_id=interaction.channel_id,
                     publication_message_id=interaction.message.id)
        except EventManagementError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        except SQLAlchemyError:
            await interaction.followup.send("❌ Could not save your RSVP. Please try again.", ephemeral=True)
            return
        await interaction.followup.send(f"✅ RSVP set to `{response}`.", ephemeral=True)
        # Discord refresh failure cannot undo an already committed RSVP.
        await self.refresh(event_id=event_id)

    async def refresh(self, event_id=None):
        # Serialize sends, then read counts from DB inside the lock, never from
        # displayed text. Concurrent button handlers queue a fresh final render.
        async with self.lock:
            try:
                with self.sessions() as session:
                    query = select(EventPublication).where(EventPublication.message_id.is_not(None))
                    if event_id is not None:
                        query = query.where(EventPublication.event_id == event_id)
                    publications = session.scalars(query.order_by(EventPublication.message_id)).all()
                live_ids = {publication.message_id for publication in publications}
                if event_id is None:
                    self.rendered = {key: value for key, value in self.rendered.items() if key in live_ids}
                for publication in publications:
                    try:
                        await self._refresh_one(publication)
                    except (discord.HTTPException, SQLAlchemyError):
                        # Missing permission/transient failure retries next cycle.
                        logger.warning("Event card refresh failed: message=%s", publication.message_id)
            except SQLAlchemyError:
                logger.warning("Event card database refresh failed; will retry")

    async def _refresh_one(self, publication):
        publication, state = read_publication_card(self.sessions, publication.message_id)
        if publication is None:
            return
        if publication.message_id in self.rendered and self.rendered[publication.message_id] == state:
            return
        guild = self.client.get_guild(publication.guild_id)
        channel = guild.get_channel(publication.channel_id) if guild else None
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != publication.guild_id:
            return
        message = channel.get_partial_message(publication.message_id)
        try:
            await message.edit(embed=card_embed(state),
                               view=RSVPView(self) if state is not None and not state.closed else None,
                               allowed_mentions=discord.AllowedMentions.none())
        except discord.NotFound:
            self._forget(publication.message_id)
            return
        if state is None:
            # A successfully disabled tombstone no longer needs ongoing retries.
            self._forget(publication.message_id)
        else:
            self.rendered[publication.message_id] = state

    def _forget(self, message_id):
        with self.sessions() as session:
            session.execute(delete(EventPublication).where(EventPublication.message_id == message_id))
            session.commit()
        self.rendered.pop(message_id, None)

    @tasks.loop(seconds=30)
    async def poll(self):
        try:
            prune_pending_publications(self.sessions)
        except SQLAlchemyError:
            logger.warning("Pending event-card cleanup failed; will retry")
        await self.publish_automatic()
        await self.refresh()

    @poll.before_loop
    async def before_poll(self):
        await self.client.wait_until_ready()

    def start(self):
        if not self.poll.is_running():
            self.poll.start()

    async def close(self):
        task = self.poll.get_task()
        self.poll.cancel()
        if task is not None:
            with suppress(asyncio.CancelledError):
                await task
