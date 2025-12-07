"""Backend for mattermost."""

import asyncio
import colorsys
import hashlib
import json
import logging
import re
import ssl
from collections import defaultdict
from collections.abc import AsyncGenerator
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import override

import aiohttp
import browser_cookie3
import truststore
import websockets
from async_lru import alru_cache
from requests.utils import dict_from_cookiejar

from kbunified.backends.interface import Channel, ChannelID, ChatBackend, Event
from kbunified.backends.slack import get_emoji

logger = logging.getLogger(__name__)

UNICODE_TO_MATTERMOST = {
    "👍": "+1",
    "👎": "-1",
    "🔥": "fire",
    "🚀": "rocket",
    "👀": "eyes",
    "✅": "white_check_mark",
    "❌": "x",
    "🎉": "tada",
}

def emoji_to_shortcode(emoji: str) -> str:
        return UNICODE_TO_MATTERMOST.get(emoji, emoji.replace(":", ""))


mention_pattern = re.compile(r"@([a-z]\d+|[a-z]+\.?[a-z]+)")

def _get_attachment_path(file_id, file_name):
        # XDG Standard: ~/.cache/kb-solaria/attachments
        base = Path.home() / ".cache" / "kb-solaria" / "attachments"
        base.mkdir(parents=True, exist_ok=True)

        # Clean filename to prevent path traversal or weird chars
        safe_name = "".join(c for c in file_name if c.isalnum() or c in "._- ")
        return base / f"{file_id}_{safe_name}"

def str_to_color(str_in):
    """Converts a UUID into a well-contrasted HTML color string.

    This function uses the HSL color model to generate a color that is
    neither too light nor too dark, making it suitable for text on
    both black and white backgrounds.

    Args:
        uuid_in: A UUID object or its string representation.

    Returns:
        A string representing the HTML color in hex format (e.g., '#80e844').
    """
    # value=0.5 ensures the color is halfway between black and white.
    # Saturation=0.7 provides a vibrant but not overly intense color.
    fixed_saturation = 0.7
    fixed_value = 0.5

    try:
        md5 = hashlib.md5(str_in.encode("utf-8"))
        md5_int = int(md5.hexdigest(), 16)
        # Use modulo to map the large integer to the 0-359 degree range for Hue.
        hue = (md5_int % 360) / 360.0
    except ValueError:
        raise ValueError("Input must be a valid UUID string or object.")

    rgb_float = colorsys.hsv_to_rgb(hue, fixed_value, fixed_saturation)
    rgb_int = tuple(int(c * 255) for c in rgb_float)
    return "#{:02x}{:02x}{:02x}".format(*rgb_int)


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

    @override
    async def login(self):
        """Log in to the service."""
        data = await self._get("users/me")
        self._user_id = data["id"]
        self._myself = data  # Store for later

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
                # FIXME: optimise this
                member = await self._get(f"channels/{channel_id}/members/me")
                last_viewed = member.get("last_viewed_at", 0) / 1000.0 # Convert ms to s
                channel = Channel(id=channel_id,
                                  name=chan["display_name"] or chan["name"],
                                  topic=chan["purpose"],
                                  unread=bool(udata["msg_count"]),
                                  mentions=udata["mention_count"],
                                  last_read_at=last_viewed
                                  )
                channels.append(channel)
        except:
            logger.exception("error in fetching channels")
        return channels

    async def fetch_all_users(self):
        """Fetch all users from Mattermost with pagination."""
        page = 0
        per_page = 200  # Mattermost max per_page is usually 200
        logger.debug(f"Fetching all users from {self.name}...")

        while True:
            try:
                # Fetch a page of users
                users = await self._get("users", page=page, per_page=per_page, active="true")

                if not users:
                    break

                # Store them in the internal cache
                for u in users:
                    self._users[u["id"]] = u

                logger.debug(f"Fetched page {page} ({len(users)} users)")

                # If we got less than the limit, we've reached the end
                if len(users) < per_page:
                    break

                page += 1
            except Exception:
                logger.exception("Failed to fetch users page")
                break

    async def _download_file(self, file_id, path):
        """Download a file from Mattermost."""
        url = f"{self._api_domain}/api/v4/files/{file_id}"
        logger.debug(f"Downloading attachment: {file_id}")

        # Replicate headers logic from _request
        headers = {}
        if "MMCSRF" in self._dict_cookies:
            headers["X-CSRF-Token"] = self._dict_cookies["MMCSRF"]

        try:
            async with self._session.get(
                url,
                headers=headers,
                cookies=self._dict_cookies
            ) as response:
                if response.status == 200:
                    with path.open("wb") as fd:
                        fd.write(await response.read())
                    logger.debug(f"Saved attachment to {path}")
                else:
                    logger.error(f"Failed to download file {file_id}: {response.status}")
        except Exception:
            logger.exception(f"Exception downloading file {file_id}")

    async def _request(self, method, endpoint, **kwargs):
        """Unified wrapper for all HTTP requests."""
        url = f"{self._api_domain}/api/v4/{endpoint}"
        logger.debug(f"Req: {method} {endpoint} | Data: {kwargs.get('json', '')}")

        # Prepare Headers (inject CSRF)
        headers = kwargs.pop("headers", {})
        if "MMCSRF" in self._dict_cookies:
            headers["X-CSRF-Token"] = self._dict_cookies["MMCSRF"]

        async with self._session.request(
            method,
            url,
            headers=headers,
            cookies=self._dict_cookies,
            **kwargs
        ) as response:
            if response.status == 204: # No Content
                data = {}
            else:
                try:
                    data = await response.json()
                except Exception:
                    logger.warning(f"Could not parse JSON from {method} {endpoint}")
                    data = {}

            if response.status not in [200, 201]:
                error_msg = data.get("error", f"HTTP {response.status}")
                logger.error(f"Failed {method} {endpoint}: {error_msg}")
                raise IOError(f"Failed {method} {endpoint}: {error_msg}")

            return data

    async def _get(self, endpoint, **params):
        return await self._request("GET", endpoint, params=params)

    async def _post(self, endpoint, payload):
        return await self._request("POST", endpoint, json=payload)

    async def _put(self, endpoint, payload):
        return await self._request("PUT", endpoint, json=payload)

    async def _delete(self, endpoint):
        return await self._request("DELETE", endpoint)

    async def events(self) -> AsyncGenerator[Event]:
        """Yield real-time events."""
        await self._login_event.wait()
        await self.connect_ws()

        # Calculate display name using the existing helper
        my_name = self._create_display_name(self._myself)

        yield self.create_event(
            event="self_info",
            user={
                "id": self._user_id,
                "name": my_name,
                "color": str_to_color(self._user_id)
            }
        )

        # 3. Then send channels
        channels = await self.get_subbed_channels()
        channel_list = self.create_event(event="channel_list",
                                         channels=[asdict(chan) for chan in channels])
        yield channel_list

        await self.fetch_all_users()

        user_list = []
        for u in self._users.values():
            user_list.append({
                "id": u["id"],
                "name": self._create_display_name(u),
                "color": str_to_color(u["id"])
            })

        yield self.create_event(event="user_list", users=user_list)

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
        except Exception:
            logger.error(str(json_event))
            logger.exception("wrong event")
            raise
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
                                            additional_headers=headers,
                                            ssl=self.ssl_ctx)
        return self._ws

    def handle_event(self, event):
        """Handle an event."""
        evt_type = event.get("event")
        data = event.get("data", {})

        if evt_type == "posted":
            post = json.loads(data["post"])
            return self.handle_message(post)

        elif evt_type == "post_edited":
            post = json.loads(data["post"])
            # We treat edits as simple updates to the body
            return self.create_event(
                event="message_update",
                message={
                    "id": post["id"],
                    "body": self.replace_mentions_in_text(post["message"])
                }
            )

        elif evt_type == "post_deleted":
            post = json.loads(data["post"])
            return self.create_event(
                event="message_delete",
                message_id=post["id"],
                channel_id=post["channel_id"]
            )

        elif evt_type in ["reaction_added", "reaction_removed"]:
            reaction = json.loads(data["reaction"])
            is_add = (evt_type == "reaction_added")

            # Map emoji name to standard if necessary (omitted for brevity)
            emoji = reaction["emoji_name"]

            return self.create_event(
                event="message_reaction",
                action="add" if is_add else "remove",
                message_id=reaction["post_id"],
                emoji=emoji,
                user_id=reaction["user_id"]
            )

        return None

    def _display_name_from_username(self, username):
        for info in self._users.values():
            if info["username"] == username:
                return self._create_display_name(info)
        raise KeyError(f"Cannot find username {username}")

    def replace_mentions_in_text(self, text):
        body = text
        for match in mention_pattern.finditer(text):
            if match.group(1) == "all":
                continue
            try:
                user_name = self._display_name_from_username(match.group(1))
            except KeyError:
                continue
            body = body.replace(match.group(0), "@" + user_name)
        return body

    def handle_message(self, post) -> Event:
        ts = post["create_at"]
        dt = datetime.fromtimestamp(ts/1000)
        try:
            user = self._users[post["user_id"]]
        except KeyError:
            return None

        display_name = self._create_display_name(user)

        # 1. Basic Message Construction
        message = dict(body=self.replace_mentions_in_text(post["message"]),
                       author=dict(id=user["id"],
                                   display_name=display_name,
                                   color=str_to_color(user["id"])
                                   ),
                       id=post["id"],
                       timestamp=ts,
                       ts_date=dt.date().isoformat(),
                       ts_time=dt.time().isoformat())

        if thread_id := post["root_id"]:
            message["thread_id"] = thread_id

        if post.get("reply_count", 0) > 0:
            message["replies"] = dict(count=post["reply_count"], users=post["participants"])

        if post.get("has_reactions"):
            reactions = defaultdict(list)
            for reaction in post["metadata"]["reactions"]:
                reactions[get_emoji(reaction["emoji_name"])].append(reaction["user_id"])
            message["reactions"] = reactions

        files = post.get("metadata", {}).get("files", [])
        if files:
            attachment_list = []
            for file_info in files:
                file_id = file_info["id"]
                file_name = file_info["name"]

                path = _get_attachment_path(file_id, file_name)

                # Download in background if missing
                if not path.exists():
                    asyncio.create_task(self._download_file(file_id, path))

                # Add structured object instead of Markdown string
                attachment_list.append({
                    "id": file_id,
                    "name": file_name,
                    "path": str(path)
                })

            # Assign to message object (do NOT touch message["body"])
            message["attachments"] = attachment_list

        event = self.create_event(event="message",
                                  channel_id=post["channel_id"],
                                  message=message)
#         print(event)
#         breakpoint()
# {'event': 'message', 'service': {'name': 'Pytroll', 'id': 'pytroll_slack'}, 'channel_id': 'C06GJDYPJ', 'message': {'body': 'good morning!', 'author': {'id': 'U06GJFRMJ', 'name': 'martin', 'color': '#9f69e7'}, 'id': '1764054914.587079', 'timestamp': 1764054914.587079, 'ts_date': '2025-11-25', 'ts_time': '08:15:14.587079'}}
# {'event': 'message', 'service': {'name': 'SMHI', 'id': 'smhi_mattermost'}, 'channel_id': 'nre6hfkosjds5ehh6ri1yudn4r', 'message': {'body': 'a001673 joined the channel.', 'author': {'id': 'zbwhwzzmei8k5poscxxfh7sfpa', 'display_name': 'Martin Raspaud', 'color': '#6fb259'}, 'id': 'a4ictnwod3r8fdhj57my7gbt1c', 'timestamp': 1741359909934, 'ts_date': '2025-03-07', 'ts_time': '16:05:09.934000'}}
        return event

    @override
    async def switch_channel(self, channel_id: ChannelID, after: str|None = None):
        params = {
            "collapsedThreads": "true",
            "collapsedThreadsExtended": "true"
        }
        if after:
            params["after"] = after

        # Fetch posts
        posts = await self._get(f"channels/{channel_id}/posts", **params)

        order = posts.get("order", [])
        if not order:
            return

        # Fetch users... (keep existing logic)
        users_list = [posts["posts"][post_id]["user_id"] for post_id in order]
        users = await self._post("users/ids", users_list)
        users_dict = {user["id"]: user for user in users}
        self._users.update(users_dict)

        for post_id in reversed(order):
            post = posts["posts"][post_id]

            event = dict(event="posted",
                         data=dict(post=json.dumps(post)))
            await self._inbox.put(json.dumps(event))


    @alru_cache
    async def fetch_thread(self, channel_id, thread_id, after: str|None = None):
        async for message in self.fetch_thread_replies(channel_id, thread_id, after):
            await self._inbox.put(message)

    async def fetch_thread_replies(self, channel_id, thread_id, after: str|None):
        """Fetch thread replies and fix the root's reply_count."""
        try:
            data = await self._get(f"posts/{thread_id}/thread")

            all_posts = data.get("order", [])
            calculated_count = len(all_posts)

            for post_id in reversed(all_posts):
                post = data["posts"][post_id]

                if post["id"] == thread_id:
                    post["reply_count"] = calculated_count - 1
                else:
                    post["reply_count"] = 0

                event = dict(event="posted",
                             data=dict(post=json.dumps(post)))
                yield json.dumps(event)
        except:
            logger.exception("Something went wrong fetching thread")
            raise

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
    async def update_message(self, channel_id: ChannelID, message_id: str, new_text: str):
        """Update a message using the new _put helper."""
        # API expects the ID in the payload as well
        payload = {"id": message_id, "message": new_text, "channel_id": channel_id}
        await self._put(f"posts/{message_id}", payload)

    @override
    async def delete_message(self, channel_id: ChannelID, message_id: str):
        """Delete a message using the new _delete helper."""
        await self._delete(f"posts/{message_id}")

    @override
    async def send_reaction(self, channel_id: ChannelID, message_id: str, reaction: str):
        """Add a reaction using the existing _post helper."""
        payload = {
            "user_id": self._user_id,
            "post_id": message_id,
            "emoji_name": emoji_to_shortcode(reaction)
        }
        await self._post("reactions", payload)

    @override
    async def remove_reaction(self, channel_id: ChannelID, message_id: str, reaction: str):
        """Remove a reaction using the new _delete helper."""
        # Endpoint: /users/{user_id}/posts/{post_id}/reactions/{emoji_name}
        endpoint = f"users/{self._user_id}/posts/{message_id}/reactions/{emoji_to_shortcode(reaction)}"
        await self._delete(endpoint)

    async def mark_channel_read(self, channel_id, message_id):
        """Mark a Slack channel as read."""
        data = await self._post(f"channels/members/{self._user_id}/view", dict(channel_id=channel_id))
        return data

    @override
    async def set_typing_status(self, channel_id: ChannelID):
        """Send a typing event via WebSocket."""
        # Mattermost expects typing events over the websocket to show up instantly
        if self._ws:
            payload = {
                "action": "user_typing",
                "seq": 1,
                "data": {
                    "channel_id": channel_id,
                    "parent_id": "" # Add thread_id support here later if needed
                }
            }
            await self._ws.send(json.dumps(payload))

    @override
    async def close(self):
        """Close the backend."""
        self._running = False
        await self._session.close()

