"""Discord transport and lifecycle for the persistent reminder processor."""

import asyncio
from contextlib import suppress
import logging

import discord
from discord.ext import tasks
from sqlalchemy.exc import SQLAlchemyError

from lastz_bot.database.session import SessionLocal
from lastz_bot.event_time import utc_now_naive, utc_to_apocalypse_time, discord_timestamp
from lastz_bot.player_reminders import PlayerReminderProcessor
from lastz_bot.rsvp_reminders import RSVPReminderProcessor
from lastz_bot.reminders import ReminderDelivery, ReminderProcessor, eligible_threshold


logger = logging.getLogger(__name__)


class ReminderWorker:
    def __init__(self, client: discord.Client) -> None:
        self.client = client
        self.processor = ReminderProcessor(SessionLocal, self.send)
        self.rsvp_processor = RSVPReminderProcessor(SessionLocal, self.send_rsvp)
        self.player_processor = PlayerReminderProcessor(SessionLocal, self.send_player)

    async def send(self, delivery: ReminderDelivery) -> None:
        delivery = self.processor.current_delivery(delivery)
        if delivery is None:
            raise RuntimeError("Reminder claim was deleted or reset")
        guild = self.client.get_guild(delivery.guild_id)
        channel = guild.get_channel(delivery.channel_id) if guild is not None else None
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != delivery.guild_id:
            raise RuntimeError("Reminder destination is unavailable in its guild")
        # No network lookup/await between the final time check and sending.
        # A claim delayed past its window is suppressed, not sent late.
        if eligible_threshold(delivery.starts_at, utc_now_naive()) != delivery.lead_minutes:
            raise RuntimeError("Reminder window has elapsed")
        starts_at = utc_to_apocalypse_time(delivery.starts_at)
        alliance = discord.utils.escape_markdown(delivery.alliance_name)
        name = discord.utils.escape_markdown(delivery.event_name)
        await channel.send(
            f"⏰ Event reminder for **{alliance}**: **{name}** "
            f"starts at `{starts_at:%Y-%m-%d %H:%M}` AT "
            f"({delivery.lead_minutes}-minute reminder).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def send_rsvp(self, delivery):
        # Resolve the private channel first: network lookups may take long enough
        # for the member to respond or an officer to change eligibility.
        user = self.client.get_user(delivery.discord_user_id)
        if user is None:
            user = await self.client.fetch_user(delivery.discord_user_id)
        if user.id != delivery.discord_user_id:
            raise RuntimeError("RSVP reminder recipient mismatch")
        channel = await user.create_dm()
        delivery = self.rsvp_processor.authorize_delivery(delivery)
        if delivery is None:
            raise RuntimeError("RSVP reminder is no longer authorized")
        name = discord.utils.escape_markdown(delivery.event_name)
        alliance = discord.utils.escape_markdown(delivery.alliance_name)
        starts = utc_to_apocalypse_time(delivery.starts_at)
        deadline = utc_to_apocalypse_time(delivery.deadline)
        # No await between final DB authorization and starting the send. As with
        # event reminders, an already in-flight request cannot be recalled.
        await channel.send(
            f"Your RSVP is still missing for **{name}** in **{alliance}**. "
            f"Event: `{starts:%Y-%m-%d %H:%M}` AT. "
            f"RSVP deadline: `{deadline:%Y-%m-%d %H:%M}` AT. "
            f"Your local time: {discord_timestamp(delivery.starts_at)}. "
            f"Please respond using the server's event card buttons or {delivery.navigation}. "
            f"This is RSVP intention, not attendance.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def send_player(self, delivery):
        user = self.client.get_user(delivery.discord_user_id)
        if user is None:
            user = await self.client.fetch_user(delivery.discord_user_id)
        if user.id != delivery.discord_user_id:
            raise RuntimeError("Player reminder recipient mismatch")
        channel = await user.create_dm()
        delivery = self.player_processor.authorize_delivery(delivery)
        if delivery is None:
            raise RuntimeError("Player reminder is no longer authorized")
        name = discord.utils.escape_markdown(delivery.event_name)[:200]
        alliance = discord.utils.escape_markdown(delivery.alliance_name)[:200]
        starts = utc_to_apocalypse_time(delivery.starts_at)
        await channel.send(
            f"Event reminder: **{name}** in **{alliance}** starts at {starts:%Y-%m-%d %H:%M} AT. "
            f"Your local time: {discord_timestamp(delivery.starts_at)}. {delivery.navigation}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @tasks.loop(seconds=30)
    async def poll(self) -> None:
        try:
            await self.processor.process_pending()
            await self.rsvp_processor.process_pending()
            await self.player_processor.process_pending()
        except SQLAlchemyError:
            # A transient database failure must not permanently stop the loop.
            # Any previously committed claim remains ineligible on the next poll.
            logger.error("Reminder database processing failed; will poll again")

    @poll.before_loop
    async def before_poll(self) -> None:
        await self.client.wait_until_ready()

    def start(self) -> None:
        if not self.poll.is_running():
            self.poll.start()

    async def close(self) -> None:
        task = self.poll.get_task()
        self.poll.cancel()
        if task is not None:
            with suppress(asyncio.CancelledError):
                await task
