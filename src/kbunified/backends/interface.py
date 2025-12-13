"""Common interface for chat backends."""

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path

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
    last_read_at: float = 0.0
    last_post_at: float = 0.0
    mass: float = 1
    category: str = "channel"

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
    async def post_message(self, channel_id: ChannelID, message_text: str, client_id: str | None = None) -> str | None:
        """Post a message."""

    @abstractmethod
    async def post_reply(self, channel_id: ChannelID, thread_id: str, message_text: str,
                         client_id: str | None = None) -> str | None:
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
    async def switch_channel(self, channel_id: ChannelID, after: str|None = None):
        """Switch to given channel."""

    @abstractmethod
    async def update_message(self, channel_id: ChannelID, message_id: str, new_text: str):
        """Update an existing message."""

    @abstractmethod
    async def remove_reaction(self, channel_id: ChannelID, message_id: str, reaction: str):
        """Remove a reaction."""

    async def set_typing_status(self, channel_id: ChannelID):
        """Indicate that the user is typing in the channel."""
        pass

    async def fetch_thread(self, channel_id: ChannelID, thread_id: str, after: str|None = None):
        """Fetch messages from a thread."""
        del channel_id, thread_id
        raise NotImplementedError

    async def mark_channel_read(self, channel_id: ChannelID, message_id: str):
        """Fetch messages from a thread."""
        del channel_id, message_id
        raise NotImplementedError

    @abstractmethod
    async def get_participated_threads(self) -> list[str]:
        """Return a list of thread IDs the user is participating in."""
        return []

    @abstractmethod
    async def close(self):
        """Close the connection to the service and shut down."""

    def create_event(self, event, **fields) -> Event:
        """Create an event from this service."""
        return create_event(event=event,
                            service=dict(name=self.name, id=self._service_id),
                            **fields)

    def get_local_cache_path(self, name, ext):
        safe_name = "".join(c for c in name if c.isalnum() or c in "_-+")
        cache_dir = Path.home() / ".cache" / "kb-solaria" / "emojis" / self._service_id
        cache_dir.mkdir(parents=True, exist_ok=True)

        return cache_dir / f"{safe_name}.{ext}"


def create_channel(channel_id: str, name: str, topic: str,
                   unread: bool = False, mentions: int = 0,
                   starred: bool = False, last_read_at: float = 0.0, category: str = "channel"):
    """Create a channel."""
    return dict(id=channel_id, name=name,
                unread=unread,
                topic=topic,
                mentions=mentions,
                starred=starred,
                last_read_at=last_read_at,
                category=category)

def create_event(event: str, service: dict[str, str], **fields: str | dict[str, str] | list[dict[str, str]]) -> Event:
    """Create an event."""
    return dict(event=event,
                service=service,
                **fields)


