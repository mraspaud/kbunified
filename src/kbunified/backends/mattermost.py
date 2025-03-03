"""Backend for mattermost."""

import logging
from collections.abc import AsyncGenerator
from typing import override

import aiohttp
import browser_cookie3
from requests.utils import dict_from_cookiejar

from kbunified.backends.interface import Channel, ChannelID, ChatBackend, Event

logger = logging.getLogger(__name__)


class MattermostBackend(ChatBackend):
    """A backend for mattermost."""

    def __init__(self, service_id: str, name: str, domain_name: str):
        """Set up the backend."""
        self._api_domain = "https://" + domain_name
        self._session = aiohttp.ClientSession()
        self._cookies = dict_from_cookiejar(browser_cookie3.firefox(domain_name=domain_name))

    async def login(self):
        """Log in to the service."""
        async with self._session.get(self._api_domain + "/api/v4/users/me",
                                     cookies=self._cookies,
                                     ) as response:
            data = await response.json()
            self._user_id = data["id"]
        async with self._session.get(self._api_domain + "/api/v4/teams",
                                     cookies=self._cookies,
                                     ) as response:
            data = await response.json()
            self._team_id = data[0]["id"]

    @override
    async def get_subbed_channels(self) -> list[Channel]:
        """Get channels the user is subbed to."""
        channels = []
        try:
            async with self._session.get(self._api_domain + f"/api/v4/users/{self._user_id}/teams/{self._team_id}/channels",
                                         cookies=self._cookies,
                                         ) as response:
                data = await response.json()
                for chan in data:
                    channel = Channel(id=chan["id"],
                                      name=chan["display_name"] or chan["name"],
                                      topic=chan["purpose"],
                                      unread=True)
                    channels.append(channel)
        except:
            logger.exception("error in fetching channels")
        return channels

    @override
    async def events(self) -> AsyncGenerator[Event]:
        """Yield real-time events."""
        event = None
        yield event

    @override
    def is_logged_in(self) -> bool:
        return super().is_logged_in()

    @override
    async def post_message(self, channel_id: ChannelID, message_text: str):
        """Post a message to the service."""
        return await super().post_message(channel_id, message_text)

    @override
    async def delete_message(self, channel_id: ChannelID, message_id: str):
        return await super().delete_message(channel_id, message_id)

    @override
    async def send_reaction(self, channel_id: ChannelID, message_id: str, reaction: str):
        return await super().send_reaction(channel_id, message_id, reaction)

    @override
    async def switch_channel(self, channel_id: ChannelID):
        return await super().switch_channel(channel_id)

    @override
    async def close(self):
        """Close the backend."""
        return await super().close()
