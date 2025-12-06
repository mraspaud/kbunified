"""Backend for Slack (using aiohttp for async operations)."""

import asyncio
import json
import logging
import re
import ssl
from collections.abc import AsyncGenerator
from dataclasses import asdict
from datetime import datetime
from functools import cache
from pathlib import Path
from pprint import pp
from typing import override

import aiohttp
import importlib_resources
import truststore
import websockets
from async_lru import alru_cache

from kbunified.backends.interface import Channel, ChannelID, ChatBackend, Event, create_event
from kbunified.utils.slack_auth import get_slack_credentials

logger = logging.getLogger("slack_backend")

ua = "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0"

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
        return code

def name_of_reaction(em):
    # Reverse lookup for sending reactions
    x = loaded_emojis()
    for name, char in x.items():
        if char == em:
            return name
    return em.replace(":", "")


emoji_pattern = re.compile(r":([a-zA-Z0-9_\-+]+):")


def replace_emoji(match):
    code = match.group(1)
    return get_emoji(code)


def replace_emojis_in_text(text):
    if not text: return ""
    return emoji_pattern.sub(replace_emoji, text)


mention_pattern = re.compile(r"<@([A-Z0-9]+)>")
post_mention_pattern = re.compile(r"@([\w]+(?:\s+[\w]+)*)")

def _get_attachment_path(file_id, file_name):
    base = Path.home() / ".cache" / "kb-solaria" / "attachments"
    base.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c for c in file_name if c.isalnum() or c in "._- ")
    return base / f"{file_id}_{safe_name}"


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
        self._user_id = None
        self.ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._session = aiohttp.ClientSession()

        # Caches
        self._message_cache = dict()
        self._user_id_name = dict()
        self._user_name_id = dict()
        self._users = dict()

        # Credentials
        logger.info("Credentials missing from keyring. Attempting auto-extraction from Firefox...")
        token, d_cookie, _ = get_slack_credentials() # Ignore ds_cookie

        if token and d_cookie:
            logger.info("Extraction successful! Saving to keyring.")
            self._session_token = token
            self._session_cookie = d_cookie
        else:
            logger.error("Auto-extraction failed.")
            raise RuntimeError("No Slack token/cookie found.")

        # WebSocket URL does require token in query usually
        self._ws_url = f"wss://wss-primary.slack.com/?token={self._session_token}"

    async def is_logged_in(self):
        """Return logged-in status."""
        return self._logged_in

    async def connect_ws(self):
        headers = {
            "Origin": "https://app.slack.com",
            "User-Agent": ua,
            "Cookie": f"d={self._session_cookie}"
        }

        logger.debug("Connecting to Slack WebSocket")
        self._ws = await websockets.connect(self._ws_url, additional_headers=headers, ssl=self.ssl_ctx)
        logger.debug("Connected")
        return self._ws

    def get_headers(self) -> dict[str, str]:
        """Get the headers for the requests."""
        return {
            "Cookie": f"d={self._session_cookie}",
            "Authorization": f"Bearer {self._session_token}",
            "User-Agent": ua,
            "Origin": "https://app.slack.com"
        }

    async def login(self):
        """Log in and verify credentials asynchronously."""
        try:
            data = await self._post("auth.test")
            self._logged_in = True
            self._user_id = data["user_id"]
            self._login_event.set()
            logger.debug(f"Logged in to Slack as {data['user']} ({data['user_id']})")
        except:
            logger.exception("login failed")

    async def _request(self, method, endpoint, **kwargs):
        """Unified request wrapper."""
        url = f"https://slack.com/api/{endpoint}"
        logger.debug(f"Req: {method} {endpoint} | Data: {kwargs.get('json', '')}")

        async with self._session.request(
            method,
            url,
            headers=self.get_headers(),
            ssl=self.ssl_ctx,
            **kwargs
        ) as response:
            try:
                data = await response.json()
            except:
                data = {}

            if not data.get("ok", False):
                error = data.get("error", "unknown")
                logger.error(f"Failed {endpoint}: {error}")
                raise IOError(f"Slack API Error: {error}")
            return data

    async def _get(self, endpoint, **params):
        return await self._request("GET", endpoint, params=params)

    async def _post(self, endpoint, **json_body):
        return await self._request("POST", endpoint, json=json_body)

    # --- ATTACHMENTS ---

    async def _download_file(self, url, path):
        try:
            async with self._session.get(url, ssl=self.ssl_ctx, headers=self.get_headers()) as resp:
                if resp.status == 200:
                    with path.open("wb") as fd:
                        fd.write(await resp.read())
                    logger.debug(f"Downloaded {path}")
        except Exception:
            logger.exception(f"Failed to download file to {path}")

    # --- ACTIONS ---

    @override
    async def post_message(self, channel_id: ChannelID, message_text: str):
        modified_body = self.replace_usernames_with_id(message_text)
        return await self._post("chat.postMessage", channel=channel_id, text=modified_body)

    @override
    async def post_reply(self, channel_id, thread_id, message_text):
        modified_body = self.replace_usernames_with_id(message_text)
        return await self._post("chat.postMessage", channel=channel_id, text=modified_body, thread_ts=thread_id)

    @override
    async def update_message(self, channel_id: ChannelID, message_id: str, new_text: str):
        # NEW!!!
        modified_body = self.replace_usernames_with_id(new_text)
        await self._post("chat.update", channel=channel_id, ts=message_id, text=modified_body)

    @override
    async def delete_message(self, channel_id: ChannelID, message_id: str):
        # NEW!!!
        await self._post("chat.delete", channel=channel_id, ts=message_id)

    @override
    async def send_reaction(self, channel_id, message_id, reaction):
        try:
            name = name_of_reaction(reaction)
        except KeyError:
            logger.error(f"'{reaction}' unknown")
            return
        await self._post("reactions.add", channel=channel_id, timestamp=message_id, name=name)

    @override
    async def remove_reaction(self, channel_id, message_id, reaction):
        # NEW!!!
        try:
            name = name_of_reaction(reaction)
        except KeyError:
            logger.error(f"'{reaction}' unknown")
            return
        await self._post("reactions.remove", channel=channel_id, timestamp=message_id, name=name)

    @override
    async def get_channel_members(self, channel_id: ChannelID) -> list[dict]:
        # NEW!!!
        try:
            data = await self._get("conversations.members", channel=channel_id, limit=200)
            member_ids = data.get("members", [])

            results = []
            for uid in member_ids:
                if uid in self._users:
                    u = self._users[uid]
                    results.append({
                        "id": u["id"],
                        "name": username(u),
                        "color": "#" + u.get("color", "cccccc")
                    })
            return results
        except Exception:
            logger.exception(f"Failed to fetch members for {channel_id}")
            return []

    # --- HELPERS ---

    def replace_usernames_with_id(self, text: str) -> str:
        return post_mention_pattern.sub(self._replace_mention_with_id, text)

    def _replace_mention_with_id(self, match: re.Match) -> str:
        mention_text = match.group(1)
        user_id = self._user_name_id.get(mention_text)
        return f"<@{user_id}>" if user_id else match.group(0)

    # --- DATA FETCHING ---

    @override
    async def get_subbed_channels(self):
        channels = []
        try:
            data = await self._post("client.userBoot", _x_reason="initial_data", version_all_channels=False,
                                    omit_channels=False, include_min_version_bumf_check=1,
                                    _x_app_name="client")

            counts = await self._post("client.counts")
            metadata = {count["id"]: count for count in counts["channels"] + counts["ims"]}

            for channel in data["channels"]:
                if channel["is_archived"]:
                    continue

                channel_id = channel["id"]
                meta = metadata.get(channel_id, {})
                name = channel["name"]
                # beautify mpdm names
                if channel.get("is_mpim"):
                    members = []
                    for member in channel["members"]:
                        user = await self.fetch_user(member)
                        members.append(username(user))
                    name = ", ".join(members)

                chan = Channel(id=channel_id,
                               name=name,
                               topic=channel["topic"]["value"],
                               unread=meta.get("has_unreads", False),
                               mentions=meta.get("mention_count", 0),
                               starred=channel_id in data.get("starred", []))
                channels.append(chan)
                logger.debug(f"Added {chan}")

            for im in data["ims"]:
                if im["is_archived"]: continue

                try:
                    target_user = self._users[im["user"]]
                    user_name = username(target_user)
                except KeyError:
                    continue

                channel_id = im["id"]
                meta = metadata.get(channel_id, {})

                chan = Channel(id=channel_id,
                               name=user_name,
                               topic=f"Conversation with {user_name}",
                               unread=meta.get("has_unreads", False),
                               mentions=meta.get("mention_count", 0),
                               starred=channel_id in data.get("starred", []))
                channels.append(chan)
                logger.debug(f"Added {chan}")

            return channels
        except:
            logger.exception("building channel list failed")
            raise

    # --- EVENTS & MAIN LOOP ---

    def create_event(self, event, **fields) -> Event:
        return create_event(event=event,
                            service=dict(name=self.name, id=self._service_id),
                            **fields)

    async def events(self) -> AsyncGenerator[Event]:
        await self._login_event.wait()
        await self.connect_ws()
        # 1. Fetch Users
        await self.fetch_all_users()

        # 2. HANDSHAKE
        myself = self._users.get(self._user_id)
        if myself:
            yield self.create_event(
                event="self_info",
                user={
                    "id": self._user_id,
                    "name": username(myself),
                    "color": "#" + myself.get("color", "cccccc")
                }
            )

        # 3. Channels
        channels = await self.get_subbed_channels()
        yield self.create_event(event="channel_list",
                                channels=[asdict(chan) for chan in channels])

        user_list = []
        for u in self._users.values():
            if u.get("deleted"):
                continue

            user_list.append({
                "id": u["id"],
                "name": username(u),
                "color": "#" + u.get("color", "cccccc")
            })

        yield self.create_event(event="user_list", users=user_list)

        # 4. Loop
        async def over_ws():
            async for event in self._ws:
                await self._inbox.put(event)

        ws_task = asyncio.create_task(over_ws())
        try:
            while self._running:
                message = await self._inbox.get()
                try:
                    data = json.loads(message)
                    event = await self.handle_event(data)
                    if event:
                        yield event
                except json.JSONDecodeError:
                    logger.debug(f"Received non-JSON message: {message}")
        finally:
            ws_task.cancel()

    async def handle_event(self, json_data) -> Event | None:
        etype = json_data["type"]

        if etype == "message":
            logger.debug(pp(json_data))
            subtype = json_data.get("subtype")

            if subtype == "message_changed":
                new_json = json_data["message"]
                new_json["channel"] = json_data["channel"]
                body = replace_emojis_in_text(new_json["text"])
                body = await self.replace_mentions_in_text(body)

                return self.create_event(
                    event="message_update",
                    message={"id": new_json["ts"], "body": body}
                )

            elif subtype == "message_deleted":
                return self.create_event(
                    event="message_delete",
                    channel_id=json_data["channel"],
                    message_id=json_data["deleted_ts"]
                )

            elif subtype == "message_replied":
                new_json = json_data["message"]
                new_json["channel"] = json_data["channel"]
                return await self.create_message_event_from_json(new_json)
            elif not subtype:
                asyncio.create_task(self.fetch_user(json_data["user"]))
                return await self.create_message_event_from_json(json_data)

        elif etype in ["reaction_added", "reaction_removed"]:
            item = json_data["item"]
            if item["type"] != "message": return None

            return self.create_event(
                event="message_reaction",
                action="add" if etype == "reaction_added" else "remove",
                message_id=item["ts"],
                channel_id=item["channel"],
                emoji=get_emoji(json_data["reaction"]),
                user_id=json_data["user"]
            )

        return None

    def register_user(self, user_info):
        uid = user_info["id"]
        dname = username(user_info)
        self._user_id_name[uid] = dname
        self._user_name_id[dname] = uid
        self._users[uid] = user_info

    async def fetch_all_users(self):
        if not self._users:
            cursor = None
            while True:
                params = {"limit": 200}
                if cursor:
                    params["cursor"] = cursor

                try:
                    data = await self._get("users.list", **params)
                    members = data.get("members", [])

                    for user in members:
                        self.register_user(user)

                    # Check for next page
                    cursor = data.get("response_metadata", {}).get("next_cursor")
                    if not cursor:
                        break

                except Exception:
                    logger.exception("Failed to fetch user page")
                    break

    async def fetch_user(self, user_id):
        if user_id not in self._users:
            data = await self._get("users.info", user=user_id)
            self.register_user(data["user"])
        return self._users[user_id]

    @alru_cache
    async def switch_channel(self, channel_id, after: str|None = None):
        """Switch channel with delta sync."""
        params = {"channel": channel_id, "limit": 50}

        if after:
            params["oldest"] = after

        try:
            data = await self._get("conversations.history", **params)
            messages = data.get("messages", [])

            # LOGGING: See if we actually got messages
            logger.debug(f"Switching to {channel_id}: Found {len(messages)} messages")

            for message in reversed(messages):
                if "channel" not in message:
                    message["channel"] = channel_id
                await self._inbox.put(json.dumps(message))

        except Exception:
            logger.exception("fetch history failed")
            raise

    @alru_cache
    async def fetch_thread(self, channel_id, thread_id, after: str|None = None):
        async for message in self.fetch_thread_replies(channel_id, thread_id):
            await self._inbox.put(message)

    async def fetch_thread_replies(self, channel_id, thread_id):
        try:
            data = await self._get("conversations.replies", channel=channel_id, ts=thread_id)
            logger.debug(f"Got thread history for {channel_id}@{thread_id}")
            messages = data.get("messages", [])
            total_replies = max(0, len(messages) - 1)

            for msg in reversed(messages):
                logger.debug(msg)
                msg["channel"] = channel_id

                yield json.dumps(msg)

        except:
            logger.exception("Something went wrong fetching thread")

    async def create_message_event_from_json(self, blob):
        try:
            # breakpoint()
            channel_id = blob["channel"]
            message_id = blob["ts"]

            user_id = blob.get("user")
            if user_id:
                user_info = await self.fetch_user(user_id)
                author = dict(id=user_info["id"],
                              display_name=username(user_info),
                              color="#" + user_info.get("color", "cccccc"))
            else:
                author = dict(id="bot", name=blob.get("username", "Bot"), color="#cccccc")

            text = blob.get("text", "")
            body = replace_emojis_in_text(text)
            body = await self.replace_mentions_in_text(body)

            timestamp = float(blob["ts"])
            dt = datetime.fromtimestamp(timestamp)
            timestamp = int(timestamp * 1000)

            message = dict(body=body,
                           author=author,
                           id=message_id,
                           timestamp=timestamp,
                           ts_date=dt.date().isoformat(),
                           ts_time=dt.time().isoformat())

            if "thread_ts" in blob and blob["thread_ts"] != blob["ts"]:
                message["thread_id"] = blob["thread_ts"]

            if "reactions" in blob:
                reactions = {}
                for rx in blob["reactions"]:
                    emoji = get_emoji(rx["name"])
                    reactions[emoji] = rx["users"]
                message["reactions"] = reactions

            if blob.get("reply_count", 0) > 0:
                message["replies"] = dict(count=blob["reply_count"])

            if "files" in blob:
                attachments = []
                for f in blob["files"]:
                    path = _get_attachment_path(f["id"], f["name"])
                    if not path.exists():
                        asyncio.create_task(self._download_file(f["url_private"], path))
                    attachments.append({
                        "id": f["id"],
                        "name": f["name"],
                        "path": str(path)
                    })
                message["attachments"] = attachments

            if blob.get("unread"):
                message["unread"] = True

            event = create_event(event="message",
                                 channel_id=channel_id,
                                 service=dict(name=self.name, id=self._service_id),
                                 message=message)
            # print(event)
            return event
        except:
            logger.exception("decoding message failed")
            raise

    async def replace_mentions_in_text(self, text):
        body = text
        for match in mention_pattern.finditer(text):
            uid = match.group(1)
            try:
                user = await self.fetch_user(uid)
                user_name = username(user)
                body = body.replace(match.group(0), "@" + user_name)
            except:
                pass
        return body

    @override
    async def close(self):
        self._running = False
        if self._session:
            await self._session.close()
        if self._ws:
            await self._ws.close()

def username(user_info):
    return user_info["profile"].get("display_name") or user_info.get("real_name") or user_info.get("name")
