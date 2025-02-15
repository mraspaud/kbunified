"""Common interface for chat backends."""

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator

type Event = dict[str, str | dict[str, str] | list[dict[str, str]]]
type Command = dict[str, str | dict[str, str]]
type ChannelID = str

class ChatBackend(ABC):
    """Chat backend."""

    @abstractmethod
    def __init__(self, service_id: str):
        """Set up the backend."""

    @abstractmethod
    async def events(self) -> AsyncGenerator[Event]:
        """Iterate over events."""

    @abstractmethod
    def is_logged_in(self) -> bool:
        """Check if the service is logged in."""

    @abstractmethod
    async def login(self):
        """Log in."""

    @abstractmethod
    async def post_message(self, channel_id: ChannelID, message: str):
        """Post a message."""

    @abstractmethod
    def get_subbed_channels(self) -> dict[ChannelID, dict[str, str]]:
        """Get the list of channels the user is subscribed to."""

    @abstractmethod
    def close(self):
        """Close the connection to the service and shut down."""


def create_event(event: str, service: dict[str, str], **fields: str | dict[str, str] | list[dict[str, str]]) -> Event:
    """Create an event."""
    return dict(event=event,
                service=service,
                **fields)


