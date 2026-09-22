import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from sqlalchemy.exc import SQLAlchemyError

from lastz_bot.reminders import ReminderDelivery
from lastz_bot.reminder_worker import ReminderWorker


class ReminderWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = Mock()
        self.client.wait_until_ready = AsyncMock()
        self.worker = ReminderWorker(self.client)
        self.delivery = ReminderDelivery(
            event_id=1, alliance_id=1, guild_id=1001, channel_id=101,
            alliance_name="Alpha", event_name="Duel @everyone",
            starts_at=datetime(2026, 9, 25, 19), lead_minutes=30,
        )
        clock = patch("lastz_bot.reminder_worker.utc_now_naive", return_value=datetime(2026, 9, 25, 18, 30))
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.channel = Mock(spec=discord.TextChannel)
        self.channel.guild = SimpleNamespace(id=1001)
        self.channel.send = AsyncMock()
        self.client.get_guild.return_value.get_channel.return_value = self.channel

    async def test_send_uses_scoped_channel_at_display_and_disables_mentions(self):
        await self.worker.send(self.delivery)
        self.client.get_guild.assert_called_once_with(1001)
        self.client.get_guild.return_value.get_channel.assert_called_once_with(101)
        self.channel.send.assert_awaited_once()
        args, kwargs = self.channel.send.call_args
        self.assertEqual(
            args[0],
            "⏰ Event reminder for **Alpha**: **Duel @everyone** starts at `2026-09-25 17:00` AT (30-minute reminder).",
        )
        self.assertEqual(kwargs["allowed_mentions"].to_dict(), {"parse": []})

    async def test_missing_guild_or_channel_never_falls_back(self):
        self.client.get_guild.return_value = None
        with self.assertRaises(RuntimeError):
            await self.worker.send(self.delivery)
        self.client.get_guild.return_value = Mock()
        self.client.get_guild.return_value.get_channel.return_value = None
        with self.assertRaises(RuntimeError):
            await self.worker.send(self.delivery)
        self.channel.send.assert_not_awaited()

    async def test_corrupt_cross_guild_channel_never_receives_event(self):
        self.channel.guild.id = 2002
        with self.assertRaises(RuntimeError):
            await self.worker.send(self.delivery)
        self.channel.send.assert_not_awaited()

    async def test_non_text_channel_is_rejected(self):
        self.client.get_guild.return_value.get_channel.return_value = Mock(spec=discord.VoiceChannel)
        with self.assertRaises(RuntimeError):
            await self.worker.send(self.delivery)
        self.channel.send.assert_not_awaited()

    async def test_stale_claim_is_not_sent_after_start_or_next_threshold(self):
        for now in (self.delivery.starts_at, self.delivery.starts_at + timedelta(seconds=1), self.delivery.starts_at - timedelta(minutes=10)):
            with self.subTest(now=now):
                self.clock.return_value = now
                with self.assertRaises(RuntimeError):
                    await self.worker.send(self.delivery)
        self.channel.send.assert_not_awaited()

    async def test_database_failure_does_not_stop_future_polls(self):
        self.worker.processor.process_pending = AsyncMock(side_effect=[SQLAlchemyError(), None])
        with self.assertLogs("lastz_bot.reminder_worker", level="ERROR"):
            await self.worker.poll()
        await self.worker.poll()
        self.assertEqual(self.worker.processor.process_pending.await_count, 2)

    async def test_loop_waits_for_ready_starts_once_and_cancels_on_close(self):
        ready = asyncio.Event()
        processed = asyncio.Event()
        self.client.wait_until_ready.side_effect = ready.wait

        async def process():
            processed.set()

        self.worker.processor.process_pending = AsyncMock(side_effect=process)
        self.worker.start()
        first_task = self.worker.poll.get_task()
        self.worker.start()
        self.assertIs(self.worker.poll.get_task(), first_task)
        self.worker.processor.process_pending.assert_not_awaited()
        ready.set()
        await asyncio.wait_for(processed.wait(), timeout=2)
        await self.worker.close()
        self.assertTrue(first_task.done())
        self.assertFalse(self.worker.poll.is_running())

    async def test_bot_starts_and_closes_worker(self):
        from lastz_bot.main import LastZBot

        bot = LastZBot()
        bot.tree.sync = AsyncMock()
        bot.reminder_worker = Mock()
        bot.reminder_worker.close = AsyncMock()
        await bot.setup_hook()
        bot.tree.sync.assert_awaited_once()
        bot.reminder_worker.start.assert_called_once()
        with patch.object(discord.Client, "close", new_callable=AsyncMock) as close:
            await bot.close()
            bot.reminder_worker.close.assert_awaited_once()
            close.assert_awaited_once()
