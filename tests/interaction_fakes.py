"""Stateful Discord acknowledgement fake: initial and followup paths stay distinct."""
from types import SimpleNamespace
from unittest.mock import AsyncMock


def transport():
    response = SimpleNamespace()
    response.is_done = lambda: response.defer.await_count > 0 or response.send_message.await_count > 0
    async def initial(*args, **kwargs):
        if response.defer.await_count + response.send_message.await_count > 1:
            raise AssertionError("Duplicate initial acknowledgement")
    response.defer = AsyncMock(side_effect=initial)
    response.send_message = AsyncMock(side_effect=initial)
    async def followup(*args, **kwargs):
        if not response.is_done():
            raise AssertionError("Followup before acknowledgement")
        return SimpleNamespace(edit=AsyncMock())
    return dict(response=response, followup=SimpleNamespace(send=AsyncMock(side_effect=followup)))
