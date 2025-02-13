"""Backend stuff."""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from queue import Empty, Queue

from typing_extensions import override

type Event = dict[str, str | dict[str, str]]
type ChannelID = str

class ChatBackend(ABC):
    """Chat backend."""

    @abstractmethod
    def __init__(self):
        """Set up the backend."""

    @abstractmethod
    async def events(self) -> AsyncGenerator[Event]:
        """Iterate over events."""

    @abstractmethod
    def is_logged_in(self) -> bool:
        """Check if the service is logged in."""

    @abstractmethod
    def login(self):
        """Log in."""

    @abstractmethod
    def post_message(self, channel_id: ChannelID, message: str):
        """Post a message."""

    @abstractmethod
    def get_subbed_channels(self) -> dict[ChannelID, dict[str, str]]:
        """Get the list of channels the user is subscribed to."""

    @abstractmethod
    def close(self):
        """Close the connection to the service and shut down."""

class DummyBackend(ChatBackend):
    """A dummy backend."""

    def __init__(self):
        """Setup the backend."""
        self._logged_in: bool = False
        self._outbox: Queue[Event] = Queue()
        self._running: bool = True

    @override
    async def events(self) -> AsyncGenerator[Event]:
        """Event generator."""
        while self._running:
            while True:
                try:
                    yield self._outbox.get_nowait()
                except Empty:
                    break
            event = dict(event_type="message",
                         message=dict(body="hello"))
            yield event
            await asyncio.sleep(.5)

    @override
    def is_logged_in(self):
        """Return logged in status."""
        return self._logged_in

    @override
    def login(self, **credentials: str):
        """Log in."""
        del credentials
        self._logged_in = True

    @override
    def post_message(self, channel_id: ChannelID, message: str):
        """Post a message."""
        event = dict(event_type="message",
                     message=dict(body=message,
                                  channel_id=channel_id))
        self._outbox.put(event)

    @override
    def get_subbed_channels(self):
        """Get the list of channels."""
        return dict(dummy_channel_1=dict(name="Dummy Channel #1"))

    @override
    def close(self):
        """Shut down the backend."""
        self._running = False


def chat_backends_from_config(**services: dict[str, object]):
    """Create a backend from the config."""
    return [DummyBackend()]
