"""Dummy chat backend."""

import asyncio
from collections.abc import AsyncGenerator
from logging import getLogger
from typing import override

from kbunified.backends import ChatBackend
from kbunified.backends.interface import ChannelID, Event, create_event

logger = getLogger("dummy_backend")

class DummyBackend(ChatBackend):
    """A dummy backend."""

    def __init__(self, service_id, name="Dummy service", username="me"):
        """Setup the backend."""
        self.service_id = service_id
        self.name = name
        self.username = username
        self._logged_in: bool = False
        self._outbox: asyncio.Queue[Event] = asyncio.Queue()
        self._running: bool = True
        self._login_event: asyncio.Event = asyncio.Event()

    @override
    async def events(self) -> AsyncGenerator[Event]:
        """Event generator."""
        logger.debug("waiting for successful login")
        _ = await self._login_event.wait()
        channel_id = "dummy_channel_1"
        channel_list = create_event(event="channel_list",
                                    service=dict(name=self.name, id=self.service_id),
                                    channels=[dict(name="Dummy Channel 1", id=channel_id)])
        logger.debug(f"sending channel list {self.name}")
        yield channel_list
        while self._running:
            while True:
                try:
                    yield self._outbox.get_nowait()
                except asyncio.QueueEmpty:
                    break
            event = create_event(event="message",
                                 channel_id=channel_id,
                                 service=dict(name=self.name, id=self.service_id),
                                 message=dict(body=f"hello {self.service_id}:{channel_id}",
                                              author="someone"))
            yield event
            await asyncio.sleep(2)

    @override
    def is_logged_in(self):
        """Return logged in status."""
        return self._logged_in

    @override
    async def login(self):
        """Log in."""
        await asyncio.sleep(0)
        self._logged_in = True
        self._login_event.set()
        logger.debug(f"logged in {self.name}")

    @override
    async def post_message(self, channel_id: ChannelID, message: str):
        """Post a message."""
        event = create_event(event="message",
                             channel_id=channel_id,
                             service=dict(name=self.name, id=self.service_id),
                             message=dict(body=message,
                                          author=self.username))
        await self._outbox.put(event)

    @override
    def get_subbed_channels(self):
        """Get the list of channels."""
        return dict(dummy_channel_1=dict(name="Dummy Channel #1"))

    @override
    def close(self):
        """Shut down the backend."""
        self._running = False


