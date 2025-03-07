"""Backend for mattermost."""

import asyncio
import json
import logging
import ssl
from collections.abc import AsyncGenerator
from dataclasses import asdict
from datetime import datetime
from typing import override

import aiohttp
import browser_cookie3
import truststore
import websockets
from requests.utils import dict_from_cookiejar

from kbunified.backends.interface import Channel, ChannelID, ChatBackend, Event

logger = logging.getLogger(__name__)


class MattermostBackend(ChatBackend):
    """A backend for mattermost."""

    def __init__(self, service_id: str, name: str, domain: str):
        """Set up the backend."""
        self.name = name
        self._service_id = service_id
        self._api_domain = "https://" + domain
        self._domain = domain
        self._session = aiohttp.ClientSession()
        self._cookies = browser_cookie3.firefox(domain_name=domain)
        self._dict_cookies = dict_from_cookiejar(self._cookies)
        self._inbox = asyncio.Queue()
        self._running = True
        self._login_event = asyncio.Event()
        self._ws = None
        self._users = dict()

    async def login(self):
        """Log in to the service."""
        data = await self._get("users/me")
        self._user_id = data["id"]

        data = await self._get("teams")
        self._team_id = data[0]["id"]

        self._login_event.set()

    @override
    async def get_subbed_channels(self) -> list[Channel]:
        """Get channels the user is subbed to."""
        channels = []
        try:
            data = await self._get(f"users/{self._user_id}/teams/{self._team_id}/channels")
            for chan in data:
                channel_id = chan["id"]
                udata = await self._get(f"users/{self._user_id}/channels/{channel_id}/unread")
                channel = Channel(id=channel_id,
                                  name=chan["display_name"] or chan["name"],
                                  topic=chan["purpose"],
                                  unread=bool(udata["msg_count"]),
                                  mentions=udata["mention_count"]
                                  )
                channels.append(channel)
        except:
            logger.exception("error in fetching channels")
        return channels

    async def _get(self, method, **params):
        """Send a get request to Mattermost."""
        logger.debug(f"Getting {method} with params {params}")
        async with self._session.get(
            f"{self._api_domain}/api/v4/{method}",
            cookies=self._dict_cookies,
            params=params
        ) as response:
            data = await response.json()
            if response.status not in [200, 201]:
                logger.error(f"Failed {method}: {data.get('error', 'unknown error')}")
                raise IOError(f"Failed {method}: {data.get('error', 'unknown error')}")
            else:
                return data

    async def _post(self, method, payload):
        """Send a get request to Mattermost."""
        logger.debug(f"Posting {method} with params {payload}")
        headers={"X-CSRF-Token": self._dict_cookies["MMCSRF"]}
        async with self._session.post(
            f"{self._api_domain}/api/v4/{method}",
            headers=headers,
            cookies=self._dict_cookies,
            json=payload
        ) as response:
            data = await response.json()
            if not response.status == 200:
                logger.error(f"Failed {method}: {data.get('error', 'unknown error')}")
                raise IOError(f"Failed {method}: {data.get('error', 'unknown error')}")
            else:
                return data

    @override
    async def events(self) -> AsyncGenerator[Event]:
        """Yield real-time events."""
        await self._login_event.wait()
        await self.connect_ws()
        channels = await self.get_subbed_channels()
        channel_list = self.create_event(event="channel_list",
                                         channels=[asdict(chan) for chan in channels])
        yield channel_list

        async def over_ws():
            async for event in self._ws:
                logger.debug(f"Received data: {event}")
                await self._inbox.put(event)
        ws_task = asyncio.create_task(over_ws())
        try:
            while self._running:
                json_event = await self._inbox.get()

                event = self.handle_event(json.loads(json_event))
                if event:
                    yield event
        except:
            logger.exception("wrong event")
        finally:
            ws_task.cancel()

    def _create_display_name(self, user):
        return user["nickname"] or (user["first_name"] + " " + user["last_name"]) or user["username"]

    async def connect_ws(self):
        headers = {
                    "Origin": self._api_domain,
                    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/132.0.0.0 Safari/537.36"),
                    "Cookie": ";".join(key+"="+val for key, val in self._dict_cookies.items())
                }
        logger.debug("Connecting to Mattermost WebSocket")

        self.ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._ws = await websockets.connect("wss://" + self._domain + "/api/v4/websocket",
                                            extra_headers=headers,
                                            ssl=self.ssl_ctx)
        return self._ws

    def handle_event(self, event):
        if event["event"] == "posted":
            return self.handle_message(json.loads(event["data"]["post"]))

    def handle_message(self, post) -> Event:
        ts = post["create_at"]
        dt = datetime.fromtimestamp(ts/1000)
        user = self._users[post["user_id"]]
        message = dict(body=post["message"],
                       author=dict(id=user["id"],
                                   display_name=self._create_display_name(user),
                                   ),
                       id=post["id"],
                       timestamp=ts,
                       ts_date=dt.date().isoformat(),
                       ts_time=dt.time().isoformat())
        if thread_id := post["root_id"]:
            message["thread_id"] = thread_id
        event = self.create_event(event="message",
                                  channel_id=post["channel_id"],
                                  message=message)
        return event

    @override
    async def switch_channel(self, channel_id: ChannelID):
        posts = await self._get(f"channels/{channel_id}/posts")
        order = posts["order"]
        users_list = [posts["posts"][post_id]["user_id"] for post_id in order]

        users = await self._post("users/ids", users_list)
        users_dict = {user["id"]: user for user in users}
        try:
            self._users.update(users_dict)

            for post_id in reversed(order):
                post = posts["posts"][post_id]
                event = dict(event="posted",
                             data=dict(post=json.dumps(post)))
                await self._inbox.put(json.dumps(event))
        except:
            logger.exception("cannot switch channel")


    @override
    def is_logged_in(self) -> bool:
        return super().is_logged_in()

    @override
    async def post_message(self, channel_id: ChannelID, message_text: str):
        """Post a message to the service."""
        await self._post("posts", dict(channel_id=channel_id, message=message_text))

    @override
    async def post_reply(self, channel_id: ChannelID, thread_id: str, message_text: str):
        """Reply to a message to the service."""
        await self._post("posts", dict(channel_id=channel_id, message=message_text, root_id=thread_id))

    @override
    async def delete_message(self, channel_id: ChannelID, message_id: str):
        return await super().delete_message(channel_id, message_id)

    @override
    async def send_reaction(self, channel_id: ChannelID, message_id: str, reaction: str):
        return await super().send_reaction(channel_id, message_id, reaction)

    @override
    async def close(self):
        """Close the backend."""
        self._running = False
        await self._session.close()
