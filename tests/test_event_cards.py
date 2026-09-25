import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import unittest

import discord
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from lastz_bot.database.models import Event, EventPublication
from lastz_bot.event_cards import CUSTOM_IDS, EventCards, RSVPView, card_embed
from lastz_bot.event_management import EventManagementError, delete_event
from lastz_bot.publications import (PENDING_PUBLICATION_TTL, abandon_publication, prune_pending_publications,
                                    read_card, record_publication, reserve_publication, resolve_publication)
from lastz_bot.recurrence import stop_series
from lastz_bot.rsvp import set_rsvp
import test_participation as participation


class EventCardTests(unittest.IsolatedAsyncioTestCase):
    connect = participation.ParticipationTests.connect
    rows = participation.ParticipationTests.rows
    interaction = participation.ParticipationTests.interaction
    once = participation.ParticipationTests.once
    series = participation.ParticipationTests.series
    rsvp = participation.ParticipationTests.rsvp
    edit = participation.ParticipationTests.edit
    edit_series = participation.ParticipationTests.edit_series
    snapshot = participation.ParticipationTests.snapshot

    def setUp(self):
        participation.ParticipationTests.setUp(self)
        clock = patch("lastz_bot.publications.utc_now_naive", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.guild = SimpleNamespace(id=1, me=SimpleNamespace(id=900))
        self.client = Mock()
        self.client.user = SimpleNamespace(id=900)
        self.messages = {}
        self.channels = {}
        self.client.get_guild.side_effect = lambda guild_id: self.guild if guild_id == 1 else None
        self.guild.get_channel = self.channels.get
        self.channel = self.add_channel(101)
        self.cards = EventCards(self.client, self.sessions)

    def add_channel(self, channel_id):
        channel = Mock(spec=discord.TextChannel)
        channel.id, channel.guild = channel_id, self.guild
        channel.permissions_for.return_value = SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)
        async def send(**kwargs):
            message = Mock()
            message.id = 1000 + len(self.messages)
            message.channel = channel
            message.author = self.client.user
            message.jump_url = f"https://discord.com/channels/1/{channel_id}/{message.id}"
            message.embed, message.view = kwargs["embed"], kwargs.get("view")
            message.initial_view = kwargs.get("view")
            async def edit(**options):
                message.embed, message.view = options["embed"], options["view"]
            message.edit = AsyncMock(side_effect=edit)
            message.delete = AsyncMock()
            self.messages[message.id] = message
            return message
        channel.send = AsyncMock(side_effect=send)
        channel.get_partial_message.side_effect = self.messages.__getitem__
        self.channels[channel_id] = channel
        return channel

    async def publish(self, occurrence, actor=10, admin=False, channel=None):
        return await self.cards.publish(self.guild, channel or self.channel, occurrence, actor, admin)

    def press(self, message, actor=30, guild=1, channel=None):
        return SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            guild=SimpleNamespace(id=guild) if guild is not None else None,
            message=message, channel_id=channel or message.channel.id,
            user=SimpleNamespace(id=actor),
        )

    def fields(self, message):
        return {field.name: field.value for field in message.embed.fields}

    def publications(self):
        with self.sessions() as session:
            return session.scalars(select(EventPublication).order_by(EventPublication.message_id)).all()

    async def test_optional_required_none_cards_and_persisted_metadata(self):
        for mode in ("optional", "required", "none"):
            occurrence = self.once(mode)
            message = await self.publish(occurrence)
            self.assertIsNone(message.initial_view)
            fields = self.fields(message)
            self.assertEqual(fields["Apocalypse Time"], "2026-09-22 17:00 AT")
            self.assertEqual(fields["Alliance"], "Alpha")
            self.assertEqual(fields["Event"], "One-time")
            self.assertEqual(fields["Going"], "0")
            self.assertIn({"optional": "optional", "required": "required", "none": "disabled"}[mode],
                          fields["Participation"])
            if mode == "none":
                self.assertIsNone(message.view)
            else:
                self.assertEqual([b.label for b in message.view.children], ["Going", "Maybe", "Not Going"])
            record = self.publications()[-1]
            self.assertEqual((record.guild_id, record.channel_id, record.message_id, record.event_id),
                             (1, 101, message.id, occurrence))

    async def test_weekly_card_contains_description_and_series_and_occurrence_ids(self):
        series = self.series()
        occurrence = self.rows(series)[0].id
        message = await self.publish(occurrence)
        self.assertEqual(message.embed.description, "Prepare")
        self.assertIn(f"Series ID {series}", self.fields(message)["Event"])
        self.assertIn(f"Occurrence ID {occurrence}", message.embed.footer.text)

    async def test_publish_management_permissions(self):
        occurrence = self.once()
        for actor, admin in ((10, False), (20, False), (999, True)):
            await self.publish(occurrence, actor, admin)
        self.assertEqual(len(self.publications()), 3)
        for actor in (30, 40, 999):
            with self.assertRaises(EventManagementError):
                await self.publish(occurrence, actor)
        self.assertEqual(len(self.publications()), 3)

    async def test_publish_cross_alliance_guild_unknown_ids_are_denied(self):
        other = self.once(alliance_id=2)
        foreign = self.once(alliance_id=3)
        for occurrence, admin in ((other, False), (foreign, False), (foreign, True), (99999, True)):
            with self.assertRaises(EventManagementError):
                await self.publish(occurrence, admin=admin)
        self.channel.send.assert_not_awaited()

    async def test_publish_channel_scope_and_permissions(self):
        occurrence = self.once()
        foreign = self.add_channel(201)
        foreign.guild = SimpleNamespace(id=2)
        with self.assertRaises(EventManagementError):
            await self.publish(occurrence, channel=foreign)
        for permission in ("view_channel", "send_messages", "embed_links"):
            permissions = SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)
            setattr(permissions, permission, False)
            self.channel.permissions_for.return_value = permissions
            with self.assertRaises(EventManagementError):
                await self.publish(occurrence)
        self.channel.send.assert_not_awaited()

    async def test_button_member_changes_response_and_counts_from_database(self):
        occurrence = self.once()
        self.rsvp(occurrence, "going", actor=20)
        message = await self.publish(occurrence)
        for response, label in (("going", "Going"), ("maybe", "Maybe"), ("not_going", "Not Going")):
            interaction = self.press(message)
            await self.cards.respond(interaction, response)
            self.assertTrue(interaction.followup.send.call_args.kwargs["ephemeral"])
            self.assertIn("✅", interaction.followup.send.call_args.args[0])
            self.assertEqual(self.fields(message)[label], "2" if response == "going" else "1")
        self.assertEqual(len(self.snapshot()), 2)

    async def test_actual_view_callback_calls_shared_service(self):
        occurrence = self.once()
        message = await self.publish(occurrence)
        await message.view.children[1].callback(self.press(message))
        self.assertEqual(self.snapshot()[0].response, "maybe")

    async def test_multiple_cards_same_occurrence_share_counts(self):
        occurrence = self.once()
        one = await self.publish(occurrence)
        two = await self.publish(occurrence, channel=self.add_channel(102))
        await self.cards.respond(self.press(one), "going")
        self.assertEqual(self.fields(one)["Going"], "1")
        self.assertEqual(self.fields(two)["Going"], "1")
        await self.cards.respond(self.press(two), "maybe")
        self.assertEqual(self.fields(one)["Maybe"], "1")
        self.assertEqual(self.fields(two)["Going"], "0")

    async def test_concurrent_buttons_final_cards_match_database(self):
        occurrence = self.once()
        one, two = await self.publish(occurrence), await self.publish(occurrence, channel=self.add_channel(102))
        original = one.edit.side_effect
        async def slow_edit(**kwargs):
            await asyncio.sleep(0)
            await original(**kwargs)
        one.edit.side_effect = slow_edit
        await asyncio.gather(
            self.cards.respond(self.press(one, actor=30), "going"),
            self.cards.respond(self.press(two, actor=20), "maybe"),
            self.cards.respond(self.press(one, actor=10), "not_going"),
        )
        expected = read_card(self.sessions, occurrence, 1).counts
        for message in (one, two):
            self.assertEqual(tuple(int(self.fields(message)[key]) for key in ("Going", "Maybe", "Not Going")), expected)

    async def test_restart_global_registration_routes_old_messages_from_persisted_association(self):
        occurrence = self.once()
        message = await self.publish(occurrence)
        self.engine.dispose()
        self.connect()
        restarted = EventCards(self.client, self.sessions)
        restarted.register()
        restarted.register()
        self.client.add_view.assert_called_once()
        restored = self.client.add_view.call_args.args[0]
        self.assertTrue(restored.is_persistent())
        self.assertIsNone(restored.timeout)
        self.assertEqual([b.custom_id for b in restored.children], list(CUSTOM_IDS.values()))
        self.assertEqual([b.custom_id for b in RSVPView(restarted).children], [b.custom_id for b in restored.children])
        await restored.children[0].callback(self.press(message))
        self.assertEqual(self.snapshot()[0].response, "going")
        self.assertEqual(self.fields(message)["Going"], "1")

    async def test_unlinked_nonmember_and_cross_guild_buttons_rejected(self):
        occurrence = self.once()
        message = await self.publish(occurrence)
        for interaction in (self.press(message, actor=40), self.press(message, actor=999),
                            self.press(message, guild=2), self.press(message, guild=None)):
            await self.cards.respond(interaction, "going")
            self.assertIn("❌", interaction.followup.send.call_args.args[0])
        self.assertEqual(self.snapshot(), [])

    async def test_forged_message_channel_and_author_are_rejected(self):
        occurrence = self.once()
        message = await self.publish(occurrence)
        forged = Mock(id=999999, author=self.client.user, channel=self.channel)
        cases = [self.press(forged), self.press(message, channel=999)]
        for interaction in cases:
            await self.cards.respond(interaction, "going")
            self.assertIn("❌", interaction.followup.send.call_args.args[0])
        message.author = SimpleNamespace(id=999)
        await self.cards.respond(self.press(message), "going")
        self.assertEqual(self.snapshot(), [])

    async def test_association_revalidated_inside_rsvp_transaction(self):
        occurrence = self.once()
        message = await self.publish(occurrence)
        other = self.once()
        for guild, channel, event_id in ((2, 101, occurrence), (1, 999, occurrence), (1, 101, other)):
            with self.assertRaises(EventManagementError):
                set_rsvp(self.sessions, guild, event_id, 30, "going",
                         publication_message_id=message.id, publication_channel_id=channel)
        self.assertEqual(self.snapshot(), [])

    async def test_participation_lifecycle_and_retained_response(self):
        occurrence = self.once()
        message = await self.publish(occurrence)
        await self.cards.respond(self.press(message), "going")
        before = self.snapshot()
        self.edit(occurrence, participation="required")
        await self.cards.poll()
        self.assertIn("required", self.fields(message)["Participation"])
        self.assertIsNotNone(message.view)
        self.edit(occurrence, participation="none")
        stale = self.press(message)
        await self.cards.respond(stale, "maybe")
        self.assertIn("disabled", stale.followup.send.call_args.args[0])
        await self.cards.poll()
        self.assertIsNone(message.view)
        self.assertIn("disabled", self.fields(message)["Participation"])
        self.assertEqual(self.snapshot(), before)
        self.edit(occurrence, participation="optional")
        await self.cards.poll()
        self.assertIsNotNone(message.view)
        await self.cards.respond(self.press(message), "maybe")
        self.assertEqual(self.fields(message)["Maybe"], "1")

    async def test_expired_cards_remove_controls_and_backend_rejects_stale_click(self):
        occurrence = self.once()
        message = await self.publish(occurrence)
        self.now = self.start
        interaction = self.press(message)
        await self.cards.respond(interaction, "going")
        self.assertIn("closed", interaction.followup.send.call_args.args[0])
        await self.cards.poll()
        self.assertIsNone(message.view)
        self.assertIn("Expired", self.fields(message)["Status"])

    async def test_cancelled_and_stopped_series_close_cards(self):
        for stopped in (False, True):
            series = self.series()
            occurrence = self.rows(series)[0].id
            message = await self.publish(occurrence)
            if stopped:
                stop_series(self.sessions, 1, series, 10, False, now=self.now)
            else:
                with self.sessions() as session:
                    session.get(Event, occurrence).status = "cancelled"
                    session.commit()
            await self.cards.respond(self.press(message), "going")
            await self.cards.poll()
            self.assertIsNone(message.view)
        self.assertEqual(self.snapshot(), [])

    async def test_new_weekly_occurrence_never_reuses_old_publication(self):
        series = self.series()
        occurrence = self.rows(series)[0].id
        message = await self.publish(occurrence)
        self.edit_series(series, time_at="18:00")
        new = self.rows(series)[-1]
        self.assertNotEqual(new.id, occurrence)
        await self.cards.poll()
        self.assertIsNone(message.view)
        self.assertEqual(self.publications()[0].event_id, occurrence)

    async def test_deleted_event_tombstone_disables_card_and_is_cleaned(self):
        occurrence = self.once()
        message = await self.publish(occurrence)
        delete_event(self.sessions, 1, occurrence, 10, False)
        self.assertIsNone(self.publications()[0].event_id)
        with self.assertRaises(EventManagementError):
            resolve_publication(self.sessions, 1, 101, message.id)
        await self.cards.poll()
        self.assertIsNone(message.view)
        self.assertIn("unavailable", message.embed.title)
        self.assertEqual(self.publications(), [])

    async def test_delete_and_id_reuse_during_publish_never_enables_replacement(self):
        occurrence = self.once()
        send = self.channel.send.side_effect
        async def replace_during_send(**kwargs):
            delete_event(self.sessions, 1, occurrence, 10, False)
            self.assertEqual(self.once(), occurrence)
            return await send(**kwargs)
        self.channel.send.side_effect = replace_during_send
        with self.assertRaises(EventManagementError):
            await self.publish(occurrence)
        self.assertEqual(self.publications(), [])
        self.assertIsNone(self.messages[1000].view)

    async def test_refresh_rechecks_binding_after_captured_publication_becomes_tombstone(self):
        occurrence = self.once()
        message = await self.publish(occurrence)
        captured = self.publications()[0]
        delete_event(self.sessions, 1, occurrence, 10, False)
        replacement = self.once(alliance_id=2)
        self.assertEqual(replacement, occurrence)
        await self.cards._refresh_one(captured)
        self.assertIn("unavailable", message.embed.title)
        self.assertIsNone(message.view)

    async def test_deleted_id_reuse_cannot_retarget_old_card(self):
        occurrence = self.once()
        message = await self.publish(occurrence)
        delete_event(self.sessions, 1, occurrence, 10, False)
        reused = self.once()
        self.assertEqual(reused, occurrence)
        await self.cards.respond(self.press(message), "going")
        self.assertEqual(self.snapshot(), [])

    async def test_missing_discord_message_removes_only_publication(self):
        occurrence = self.once()
        message = await self.publish(occurrence)
        message.edit.side_effect = discord.NotFound(Mock(status=404, reason="Not Found"), {"message": "gone", "code": 10008})
        self.rsvp(occurrence)
        await self.cards.refresh()
        self.assertEqual(self.publications(), [])
        self.assertEqual(len(self.snapshot()), 1)
        self.assertEqual(len(self.rows()), 1)

    async def test_failed_edit_preserves_valid_rsvp_and_retries(self):
        occurrence = self.once()
        message = await self.publish(occurrence)
        edit = message.edit.side_effect
        message.edit.side_effect = discord.Forbidden(Mock(status=403, reason="Forbidden"), {"message": "denied", "code": 50013})
        with self.assertLogs("lastz_bot.event_cards", level="WARNING"):
            await self.cards.respond(self.press(message), "going")
        self.assertEqual(len(self.snapshot()), 1)
        self.assertEqual(len(self.publications()), 1)
        message.edit.side_effect = edit
        await self.cards.poll()
        self.assertEqual(self.fields(message)["Going"], "1")

    async def test_publish_persistence_failure_never_enables_controls(self):
        occurrence = self.once()
        with patch("lastz_bot.event_cards.record_publication", side_effect=EventManagementError("changed")):
            with self.assertRaises(EventManagementError):
                await self.publish(occurrence)
        message = self.messages[1000]
        self.assertIsNone(message.view)
        message.delete.assert_awaited_once()
        self.assertEqual(self.publications(), [])

    async def test_uncertain_send_not_retried_or_persisted(self):
        occurrence = self.once()
        self.channel.send.side_effect = discord.HTTPException(Mock(status=500, reason="Failure"), "failure")
        with self.assertRaises(EventManagementError):
            await self.publish(occurrence)
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.publications(), [])

    async def test_interrupted_publish_reservation_expires_without_retry_after_restart(self):
        occurrence = self.once()
        self.channel.send.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.publish(occurrence)
        reserved, = self.publications()
        self.assertIsNone(reserved.message_id)
        self.engine.dispose()
        self.connect()
        restarted = EventCards(self.client, self.sessions)
        self.now += PENDING_PUBLICATION_TTL
        await restarted.poll()
        self.assertEqual(self.publications(), [])
        self.channel.send.assert_awaited_once()
        with self.assertRaises(EventManagementError):
            record_publication(self.sessions, reserved.id, 5555, 10, False)

    async def test_pending_cleanup_keeps_recent_reservations_and_registered_cards(self):
        occurrence = self.once()
        message = await self.publish(occurrence)
        old, _ = reserve_publication(self.sessions, 1, occurrence, 101, 10, False)
        self.now += PENDING_PUBLICATION_TTL
        recent, _ = reserve_publication(self.sessions, 1, occurrence, 101, 10, False)
        prune_pending_publications(self.sessions)
        records = self.publications()
        self.assertNotIn(old, [r.id for r in records])
        self.assertIn(recent, [r.id for r in records])
        self.assertIn(message.id, [r.message_id for r in records])

    async def test_reservation_id_never_reused_after_cleanup(self):
        occurrence = self.once()
        old, _ = reserve_publication(self.sessions, 1, occurrence, 101, 10, False)
        abandon_publication(self.sessions, old)
        new, _ = reserve_publication(self.sessions, 1, occurrence, 101, 10, False)
        self.assertGreater(new, old)
        with self.assertRaises(EventManagementError):
            record_publication(self.sessions, old, 5555, 10, False)
        record_publication(self.sessions, new, 6666, 10, False)
        self.assertEqual(self.publications()[0].message_id, 6666)
        with self.assertRaises(EventManagementError):
            record_publication(self.sessions, new, 7777, 10, False)
        self.assertEqual(self.publications()[0].message_id, 6666)

    async def test_db_cleanup_failure_does_not_skip_discord_cleanup(self):
        occurrence = self.once()
        with patch("lastz_bot.event_cards.record_publication", side_effect=SQLAlchemyError("unavailable")), \
                patch("lastz_bot.event_cards.abandon_publication", side_effect=SQLAlchemyError("unavailable")):
            with self.assertRaises(EventManagementError):
                await self.publish(occurrence)
        message = self.messages[1000]
        message.delete.assert_awaited_once()
        self.assertIsNone(message.view)
        self.assertIsNone(self.publications()[0].message_id)
        self.now += PENDING_PUBLICATION_TTL
        await self.cards.poll()
        self.assertEqual(self.publications(), [])

    async def test_failed_orphan_message_deletion_leaves_no_usable_controls(self):
        occurrence = self.once()
        send = self.channel.send.side_effect
        async def send_inert(**kwargs):
            message = await send(**kwargs)
            message.delete.side_effect = discord.Forbidden(Mock(status=403, reason="Forbidden"), "denied")
            return message
        self.channel.send.side_effect = send_inert
        with patch("lastz_bot.event_cards.record_publication", side_effect=SQLAlchemyError("unavailable")):
            with self.assertRaises(EventManagementError):
                await self.publish(occurrence)
        self.assertEqual(self.publications(), [])
        self.assertIsNone(self.messages[1000].view)
        with self.assertRaises(EventManagementError):
            resolve_publication(self.sessions, 1, 101, 1000)

    async def test_slash_fallback_rsvp_and_summary_still_work_and_poll_updates_card(self):
        occurrence = self.once()
        message = await self.publish(occurrence)
        interaction = self.interaction(actor=30)
        await self.commands["rsvp"](interaction, occurrence, "going")
        await self.cards.poll()
        self.assertEqual(self.fields(message)["Going"], "1")
        manager = self.interaction()
        await self.commands["rsvps"](manager, occurrence)
        self.assertIn("Going (1)", manager.response.send_message.call_args.args[0])

    async def test_publish_command_is_explicit_and_ephemeral(self):
        occurrence = self.once()
        self.assertEqual(self.publications(), [])
        interaction = self.press(Mock(channel=self.channel))
        interaction.guild = self.guild
        interaction.user = SimpleNamespace(id=10, guild_permissions=SimpleNamespace(administrator=False))
        interaction.client = SimpleNamespace(event_cards=self.cards)
        await self.commands["publish"](interaction, occurrence, self.channel)
        self.assertTrue(interaction.followup.send.call_args.kwargs["ephemeral"])
        self.assertIn("published", interaction.followup.send.call_args.args[0])

    async def test_bot_registers_and_starts_cards_and_closes_them(self):
        from lastz_bot.main import LastZBot
        bot = LastZBot()
        bot.tree.sync = AsyncMock()
        bot.reminder_worker = Mock(close=AsyncMock())
        bot.event_cards = Mock(close=AsyncMock())
        await bot.setup_hook()
        bot.event_cards.register.assert_called_once()
        bot.event_cards.start.assert_called_once()
        with patch.object(discord.Client, "close", new_callable=AsyncMock):
            await bot.close()
        bot.event_cards.close.assert_awaited_once()
