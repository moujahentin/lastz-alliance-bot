"""Discord acknowledgement and delivery, separate from business transactions.

Never retry a callback when transport fails: it may already have committed.
Autocomplete and persistent card components deliberately do not use this adapter.
"""
from functools import wraps
from inspect import signature
import logging

import discord
from sqlalchemy.exc import SQLAlchemyError

log = logging.getLogger(__name__)


def delivery_failure(interaction, phase, error):
    # Exception text/payloads can contain user data; log only transport metadata.
    log.warning("Interaction %s failed: guild=%s interaction=%s status=%s code=%s",
                phase, interaction.guild_id, interaction.id,
                getattr(error, "status", None), getattr(error, "code", None))


async def acknowledge(interaction):
    if interaction.response.is_done():
        return True
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
        return True
    except discord.InteractionResponded:
        return True
    except discord.HTTPException as error:
        delivery_failure(interaction, "acknowledgement (operation not started)", error)
        return False


async def respond(interaction, content, *, ephemeral=True, **kwargs):
    """Send once through the appropriate Discord path; a failure is not a DB failure."""
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(content, ephemeral=ephemeral, **kwargs)
        return await interaction.response.send_message(content, ephemeral=ephemeral, **kwargs)
    except (discord.HTTPException, discord.InteractionResponded) as error:
        delivery_failure(interaction, "response delivery (operation not retried)", error)
        return None


def private_command(callback):
    """Acknowledge database-backed commands before any potentially blocking work."""
    interaction_index = 1 if next(iter(signature(callback).parameters)) == "self" else 0
    @wraps(callback)
    async def wrapped(*args, **kwargs):
        interaction = args[interaction_index] if len(args) > interaction_index else kwargs["interaction"]
        if not await acknowledge(interaction):
            return
        try:
            return await callback(*args, **kwargs)
        except SQLAlchemyError:
            # Do not claim rollback: a later read could fail after a prior commit.
            log.error("Database operation could not be confirmed: guild=%s interaction=%s",
                      interaction.guild_id, interaction.id)
            await respond(interaction, "❌ I could not confirm the result. Check the current state before trying again.")
    return wrapped
