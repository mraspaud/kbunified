"""Backend for Slack (using aiohttp for async polling)."""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from contextlib import suppress
from datetime import datetime
from functools import cache
from pathlib import Path
from tempfile import gettempdir

import aiohttp
import importlib_resources
import keyring
import websockets
from async_lru import alru_cache

from kbunified.backends.interface import ChatBackend, Event, create_event

logger = logging.getLogger("slack_backend")

@cache
def loaded_emojis():
    """Load emojis from file."""
    resources = importlib_resources.files("kbunified")
    return json.loads(resources.joinpath("data", "slack_emojis.json").read_text())

def get_emoji(code):
    """Get emoji corresponding to code."""
    emojis = loaded_emojis()
    try:
        return emojis[code]
    except KeyError:
        logger.info(f"Cannot find emoji for {code}")
        return f":{code}:"

import re

emoji_pattern = re.compile(r":([a-zA-Z0-9_\-+]+):")


def replace_emoji(match):
    code = match.group(1)
    return get_emoji(code)  # get_emoji looks up the emoji from your loaded_emojis


def replace_emojis_in_text(text):
    return emoji_pattern.sub(replace_emoji, text)


mention_pattern = re.compile(r"<@([A-Z0-9]+)>")


class SlackBackend(ChatBackend):
    """The Slack backend (async polling-based, no Socket Mode)."""

    def __init__(self, service_id, name):
        self._service_id = service_id
        self.name = name
        self._logged_in = False
        self._login_event = asyncio.Event()
        self._inbox = asyncio.Queue()
        self._running = True
        self._ws = None
        self._current_thread_id = None

        # Retrieve the stored user token from the keyring
        self._session_token = keyring.get_password("kbunified-slack", "session_token")
        if not self._session_token:
            raise RuntimeError("No Slack token found.")

        self._session_cookie = keyring.get_password("kbunified-slack", "session_cookie")
        if not self._session_cookie:
            raise RuntimeError("No Slack cookie found.")

        extra_args = ""
        self._ws_url = f"wss://wss-primary.slack.com/?token={self._session_token}{extra_args}"

        self._session = aiohttp.ClientSession()
        self._message_cache = dict()

    async def is_logged_in(self):
        """Return logged-in status."""
        return self._logged_in

    async def connect_ws(self):
        headers = {
                    "Origin": "https://app.slack.com",
                    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/132.0.0.0 Safari/537.36"),
                    "Cookie": f"d={self._session_cookie}"
                }

        logger.debug("Connecting to Slack WebSocket")
        self._ws = await websockets.connect(self._ws_url, extra_headers=headers)
        return self._ws

    def get_headers(self) -> dict[str, str]:
        return {"Cookie": f"d={self._session_cookie}",
                "Authorization": f"Bearer {self._session_token}"}

    async def login(self):
        """Log in and verify credentials asynchronously."""
        async with self._session.get(
                "https://api.slack.com/api/auth.test",
                headers=self.get_headers(),
            ) as response:
                data = await response.json()
                if data["ok"]:
                    self._logged_in = True
                    self._login_event.set()
                    logger.debug(f"Logged in to Slack as {data['user']} ({data['user_id']})")



    async def post_message(self, channel_id, message):
        """Post a message to a Slack channel."""
        logger.debug(f"Posting message to {channel_id}: {message}")
        async with self._session.post(
            "https://api.slack.com/api/chat.postMessage",
            headers=self.get_headers(),
            json={"channel": channel_id, "text": message}
        ) as response:
            data = await response.json()
            if not data.get("ok", False):
                logger.error(f"Failed to send message: {data.get('error', 'unknown error')}")
            return data


    async def get_subbed_channels(self):
        """Get the list of Slack channels the user is in."""
        channels = []
        try:
            async with self._session.post(
                    "https://api.slack.com/api/client.userBoot",
                    headers=self.get_headers(),
                    json={"_x_reason": "initial_data",
                          "version_all_channels": False,
                          "omit_channels": False,
                          "include_min_version_bump_check": 1,
                          "_x_app_name": "client",
                          }) as response:
                data = await response.json()
                if not data["ok"]:
                    logger.error("Failed to fetch boot package")

            async with await self._session.post("https://api.slack.com/api/client.counts",
                                               headers=self.get_headers()) as response:
                counts = await response.json()
                if not data["ok"]:
                    logger.error("Failed to fetch counts")
                metadata = {count["id"]: count for count in counts["channels"]}

            for channel in data["channels"]:
                if channel["is_archived"]:
                    continue

                channel_id = channel["id"]
                unread = metadata.get(channel_id, dict()).get("has_unreads", False)
                mentions = metadata.get(channel_id, dict()).get("mention_count", 0)
                starred = channel_id in data.get("starred", list())

                chan = dict(id=channel_id, name=channel["name"],
                            unread=unread,
                            topic=channel["topic"]["value"],
                            mentions=mentions,
                            starred=starred)
                logger.debug(f"added {chan}")
                channels.append(chan)
            return channels
        except:
            logger.exception("building channel list failed")
            raise
        # response = await self._client.conversations_list(types="public_channel,private_channel,im,mpim")
        # response = await self._client.conversations_list(types="im", exclude_archived=True)
        response = await self._client.users_conversations(types="im", exclude_archived=True)
        logger.debug(type(response))
        if response.get("ok", False):
            logger.debug(response["channels"])
            for channel in response["channels"]:
                if channel["id"] not in ["D19QZ45RR", "D04V4HT7XT2"]:
                    continue
                try:
                    chan = dict(id=channel["id"], name=channel["name"], topic=channel.get("topic"))
                except KeyError:
                    logger.debug(f"ok {channel}")
                    chan = dict(id=channel["id"], name=channel["user"])
                channels.append(chan)
                        # if channel["is_member"] and not channel["is_archived"]]
        return channels

    async def events(self) -> AsyncGenerator[Event]:
        """Event generator (polling for new messages)."""
        logger.debug("Slack backend ready to roll")
        await self._login_event.wait()

        await self.connect_ws()

        # Fetch channels
        channels = await self.get_subbed_channels()

        channel_list = create_event(event="channel_list",
                                    service=dict(name=self.name, id=self._service_id),
                                    channels=channels)
        yield channel_list

        # async for message in self.fetch_messages("C0LNH7LMB"):
        #     yield message
        async def over_ws():
            async for event in self._ws:
                await self._inbox.put(event)
        ws_task = asyncio.create_task(over_ws())
        try:
            while self._running:
                message = await self._inbox.get()
                # Process each incoming message
                try:
                    data = json.loads(message)
                    logger.debug(f"Received data: {data}")
                    event = await self.handle_event(data)
                    if event:
                        yield event
                except json.JSONDecodeError:
                    logger.debug(f"Received non-JSON message: {message}")
        finally:
            ws_task.cancel()

    async def handle_event(self, json_data) -> Event | None:
        """Handle data dictionary to form events."""
        if json_data["type"] == "message":
            if json_data.get("subtype") == "message_replied":  # a regular message is following this
                # self._current_thread_id = json_data["message"]["client_msg_id"]
                # self._current_thread_id = json_data["message"]["ts"]
                # user = json_data["message"]["user"]
                json_data["message"]["channel"] = json_data["channel"]
                return await self.handle_event(json_data["message"])
            elif json_data.get("subtype") == "message_deleted":
                return self.delete_message(json_data)
            elif json_data.get("subtype") == "message_changed":
                json_data["message"]["channel"] = json_data["channel"]
                return await self.handle_event(json_data["message"])
            elif json_data.get("subtype"):
                logger.debug(f"Ignoring (unknown subtype): {json_data}")
                return
            user = json_data["user"]
            asyncio.create_task(self.fetch_user(user))
            return await self.create_message_from_blob(json_data)
        if json_data["type"] == "reaction_added":
            return self.react_to_message(json_data)
        if json_data["type"] == "reaction_removed":
            return self.unreact_to_message(json_data)

    @alru_cache(maxsize=2048)
    async def fetch_user(self, user_id):
        async with self._session.post(
                f"https://api.slack.com/api/users.info?user={user_id}",
                headers=self.get_headers()) as response:
            data = await response.json()
            if not data["ok"]:
                logger.error("Failed to fetch user info")
            return data["user"]

    @alru_cache
    async def switch_channel(self, channel_id, limit=20):
        async for message in self.fetch_messages(channel_id, limit):
            await self._inbox.put(message)

    async def fetch_messages(self,  channel_id, limit=20):
        """Poll Slack for new messages asynchronously."""
        try:
            async with self._session.get(
                "https://api.slack.com/api/conversations.history",
                headers=self.get_headers(),
                params={"channel": channel_id, "limit": limit}
            ) as response:
                data = await response.json()
                logger.debug(f"Got conversation history for {channel_id}")
                if data.get("ok", False) and data.get("messages", []):
                    for message in reversed(data["messages"]):
                        logger.debug(message)
                        if "channel" not in message:
                            message["channel"] = channel_id
                        # if "subtype" not in message:  # Ignore bot/system messages
                        yield json.dumps(message)
        except:
            logger.exception("Something went wrong fetching history")
            raise

    @alru_cache
    async def fetch_thread(self, channel_id, thread_id):
        async for message in self.fetch_thread_replies(channel_id, thread_id):
            await self._inbox.put(message)

    async def fetch_thread_replies(self,  channel_id, thread_id):
        """Poll Slack for new replies asynchronously."""
        try:
            async with self._session.get(
                "https://api.slack.com/api/conversations.replies",
                headers=self.get_headers(),
                params={"channel": channel_id, "ts": thread_id}
            ) as response:
                data = await response.json()
                logger.debug(f"Got thread history for {channel_id}@{thread_id}")
                if data.get("ok", False) and data.get("messages", []):
                    for message in reversed(data["messages"]):
                        logger.debug(message)
                        if "channel" not in message:
                            message["channel"] = channel_id
                        # if "subtype" not in message:  # Ignore bot/system messages
                        yield json.dumps(message)
        except:
            logger.exception("Something went wrong fetching thread")
            raise

    def delete_message(self, blob):
        event = create_event(event="deleted_message",
                             service=dict(name=self.name, id=self._service_id),
                             channel_id=blob["channel"],
                             message=dict(id=blob["deleted_ts"]))
        return event

    async def download_file(self, attachment, path):
        resp = await self._session.get(attachment["url_private"],
                                       headers=self.get_headers())
        with path.open("wb") as fd:
            fd.write(await resp.read())

    async def create_message_from_blob(self, blob):
        """Convert a Slack message event into a unified message event."""
        try:
            channel_id = blob["channel"]
            # ts in the id, not msg_client_id
            message_id = blob["ts"]
            user_info = await self.fetch_user(blob["user"])
            author = dict(id=user_info["id"], username=user_info["name"],
                          display_name=user_info["profile"]["display_name"] or user_info["real_name"],
                          color="#" + user_info["color"])
            body = replace_emojis_in_text(blob["text"])
            body = await self.replace_mentions_in_text(body)

            timestamp = float(blob["ts"])
            ts_date, ts_time = self.blob_to_time(timestamp)

            message = dict(body=body,
                           author=author,
                           id=message_id,
                           ts_date=ts_date,
                           ts_time=ts_time)

            with suppress(KeyError):
                message["thread_id"] = blob["thread_ts"]
            if blob.get("reactions"):
                message["reactions"] = {get_emoji(reaction["name"]): reaction["users"] for reaction in blob["reactions"]}
            if blob.get("reply_count", 0) > 0:
                message["replies"] = dict(count=blob["reply_count"], users=blob["reply_users"])
            with suppress(KeyError):
                bfiles = blob["files"]
                files = list()
                for attachment in bfiles:
                    path = Path(gettempdir()) / (attachment["id"] + "_" + attachment["name"].replace(" ", "_"))
                    if not path.exists():
                        asyncio.create_task(self.download_file(attachment, path))

                    files.append(f"![{attachment["title"]}]({str(path)})")
                message["body"] += "\n" + "\n".join(files)
            if "edited" in blob:
                blob_dt = float(blob["edited"]["ts"])
                edt = self.blob_to_time(blob_dt)
                message["edit_date"], message["edit_time"] = edt

            event = create_event(event="message",
                                 channel_id=channel_id,
                                 service=dict(name=self.name, id=self._service_id),
                                 message=message)

            index = (blob["channel"], blob["ts"])
            self._message_cache[index] = event
            return event
        except:
            logger.exception("decoding message failed")
            raise

    async def replace_mentions_in_text(self, text):
        body = text
        for match in mention_pattern.finditer(text):
            user = await self.fetch_user(match.group(1))
            user_name = user["profile"]["display_name"] or user["profile"]["real_name"]
            body = body.replace(match.group(0), "@" + user_name)
        return body

    def react_to_message(self, blob):
        index = (blob["item"]["channel"], blob["item"]["ts"])
        event = self._message_cache.get(index)
        if not event:
            return None
        reaction_emoji = get_emoji(blob["reaction"])
        reactions = event["message"].setdefault("reactions", dict())
        reaction_users = reactions.setdefault(reaction_emoji, list())
        reaction_users.append(blob["user"])
        return event

    def unreact_to_message(self, blob):
        index = (blob["item"]["channel"], blob["item"]["ts"])
        event = self._message_cache.get(index)
        if not event:
            return None
        reaction_emoji = get_emoji(blob["reaction"])
        event["message"]["reactions"][reaction_emoji].remove(blob["user"])
        if not event["message"]["reactions"][reaction_emoji]:
            event["message"]["reactions"].pop(reaction_emoji)
        if not event["message"]["reactions"]:
            event["message"].pop("reactions")
        return event

    async def close(self):
        """Shut down the backend and close the aiohttp session."""
        self._running = False
        if self._session:
            await self._session.close()
        if self._ws:
            self._ws.close()

    @staticmethod
    def blob_to_time(timestamp):
        """Convert Slack timestamp (epoch format) to a formatted date/time."""
        dt = datetime.fromtimestamp(timestamp)

        return dt.date().isoformat(), dt.time().isoformat()


"""
todo
- display name
- signal new message in inactive channels
- reconnect url
- typing detection


some test messages

"""

