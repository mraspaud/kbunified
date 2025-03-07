"""Common interface for chat backends."""

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass

type Event = dict[str, str | dict[str, str] | list[dict[str, str]]]
type Command = dict[str, str | dict[str, str]]
type ChannelID = str

@dataclass
class Channel:
    """A channel."""
    id: str
    name: str
    topic: str = ""
    unread: bool = False
    mentions: int = 0
    starred: bool = False

class ChatBackend(ABC):
    """Chat backend."""

    @abstractmethod
    def __init__(self, service_id: str, service_name: str):
        """Set up the backend."""
        self._service_id = service_id
        self.name = service_name

    @abstractmethod
    async def login(self):
        """Log in.

        This method ensures the user is logged and has all the basic information to continue.
        """

    @abstractmethod
    def is_logged_in(self) -> bool:
        """Check if the service is logged in."""

    @abstractmethod
    async def get_subbed_channels(self) -> list[Channel]:
        """Get the list of channels the user is subscribed to.

        This method retrieves the list of channels from the service.
        """

    @abstractmethod
    async def post_message(self, channel_id: ChannelID, message_text: str):
        """Post a message."""

    @abstractmethod
    async def post_reply(self, channel_id: ChannelID, thread_id: str, message_text: str):
        """Reply to a message."""

    @abstractmethod
    async def send_reaction(self, channel_id: ChannelID, message_id: str, reaction: str):
        """React to a message."""

    @abstractmethod
    async def delete_message(self, channel_id: ChannelID, message_id: str):
        """Delete a message."""

    @abstractmethod
    async def events(self) -> AsyncGenerator[Event]:
        """Iterate over events."""

    @abstractmethod
    async def switch_channel(self, channel_id: ChannelID):
        """Switch to given channel."""

    @abstractmethod
    async def close(self):
        """Close the connection to the service and shut down."""

    def create_event(self, event, **fields) -> Event:
        """Create an event from this service."""
        return create_event(event=event,
                            service=dict(name=self.name, id=self._service_id),
                            **fields)

def create_channel(channel_id: str, name: str, topic: str,
                   unread: bool = False, mentions: int = 0, starred: bool = False):
    """Create a channel."""
    return dict(id=channel_id, name=name,
                unread=unread,
                topic=topic,
                mentions=mentions,
                starred=starred)

def create_event(event: str, service: dict[str, str], **fields: str | dict[str, str] | list[dict[str, str]]) -> Event:
    """Create an event."""
    return dict(event=event,
                service=service,
                **fields)


