"""Backend for Slack (using aiohttp for async operations)."""

import asyncio
import json
import logging
import re
import ssl
from collections.abc import AsyncGenerator
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from pprint import pp
from typing import override

import aiohttp
import truststore
import websockets
from async_lru import alru_cache

from kbunified.backends.interface import Channel, ChannelID, ChatBackend, Event, create_event
from kbunified.utils.emoji import EmojiManager
from kbunified.utils.slack_auth import get_slack_credentials

logger = logging.getLogger("slack_backend")

ua = "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0"


mention_pattern = re.compile(r"<@([A-Z0-9]+)>")
post_mention_pattern = re.compile(r"@([a-zA-Z0-9_\-\.\(\)]+(?:\s+[a-zA-Z0-9_\-\.\(\)]+)*)")

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
        self.emojis = EmojiManager()

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
        # logger.debug(f"Req: {method} {endpoint} | Data: {kwargs.get('json', '')}")

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


    @override
    async def post_message(self, channel_id: ChannelID, message_text: str, client_id: str | None = None) -> str | None:
        modified_body = self.replace_usernames_with_id(message_text)
        kwargs = {"channel": channel_id, "text": modified_body}
        if client_id:
            kwargs["client_msg_id"] = client_id # Worth a try

        data = await self._post("chat.postMessage", **kwargs)
        return data.get("ts")

    @override
    async def post_reply(self, channel_id: ChannelID, thread_id: str, message_text: str,
                         client_id: str | None = None) -> str | None:
        modified_body = self.replace_usernames_with_id(message_text)
        kwargs = {"channel": channel_id, "text": modified_body, "thread_ts": thread_id}
        if client_id:
            kwargs["client_msg_id"] = client_id

        data = await self._post("chat.postMessage", **kwargs)
        return data.get("ts")

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
            name = self.emojis.get_shortcode(reaction)
        except KeyError:
            logger.error(f"'{reaction}' unknown")
            return
        await self._post("reactions.add", channel=channel_id, timestamp=message_id, name=name)

    @override
    async def remove_reaction(self, channel_id, message_id, reaction):
        # NEW!!!
        try:
            name = self.emojis.get_shortcode(reaction)
        except KeyError:
            logger.error(f"'{reaction}' unknown")
            return
        await self._post("reactions.remove", channel=channel_id, timestamp=message_id, name=name)


    async def mark_channel_read(self, channel_id, message_id):
        """Mark a Slack channel as read."""
        await self._post("conversations.mark", channel=channel_id, ts=message_id)

    @override
    async def set_typing_status(self, channel_id: ChannelID):
        # Slack HTTP API does not support sending typing indicators easily.
        pass

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
        original_text = match.group(1) # e.g. "Alice Hello"

        # 1. Try exact match first (Optimization)
        if original_text in self._user_name_id:
            return f"<@{self._user_name_id[original_text]}>"

        # 2. Backtracking Loop
        # Split "Alice Hello World" -> ["Alice", "Hello", "World"]
        parts = original_text.split()

        # Try: "Alice Hello World", then "Alice Hello", then "Alice"
        for i in range(len(parts), 0, -1):
            candidate = " ".join(parts[:i])
            remainder = " ".join(parts[i:])

            if candidate in self._user_name_id:
                user_id = self._user_name_id[candidate]
                # Reconstruct: <@ID> + space + remainder
                if remainder:
                    return f"<@{user_id}> {remainder}"
                return f"<@{user_id}>"

        # 3. No match found
        return match.group(0)

    # --- DATA FETCHING ---

    @override
    async def get_subbed_channels(self):
        channels = []
        try:
            member_counts = {}
            cursor = None
            while True:
                # Fetch public and private channels to get 'num_members'
                kwargs = dict()
                if cursor:
                    kwargs["cursor"] = cursor
                convs = await self._get(
                    "conversations.list",
                    types="public_channel,private_channel,mpim,im",
                    limit=1000,
                    exclude_archived="true",
                    **kwargs
                )
                for c in convs.get("channels", []):
                    member_counts[c["id"]] = c.get("num_members", 1)

                cursor = convs.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break

            data = await self._post("client.userBoot", _x_reason="initial_data", version_all_channels=False,
                                    omit_channels=False, include_min_version_bumf_check=1, include_num_members=True,
                                    _x_app_name="client")

            counts = await self._post("client.counts")
            metadata = {count["id"]: count for count in counts["channels"] + counts["ims"] + counts["mpims"]}
            total_pop = len(self._users) if self._users else 1

            for channel in data["channels"]:
                if channel["is_archived"]:
                    continue

                channel_id = channel["id"]
                meta = metadata.get(channel_id, {})
                last_read = float(meta.get("last_read", channel.get("last_read", 0)))
                name = channel["name"]
                # beautify mpdm names
                if channel.get("is_mpim"):
                    members = []
                    for member in channel["members"]:
                        user = await self.fetch_user(member)
                        members.append(username(user))
                    name = ", ".join(members)

                last_post = meta.get("latest", 0)
                if last_post == 0:
                    last_post = channel["created"]
                else:
                    last_post = float(last_post)
                if last_read <= 1 and not meta.get("has_unreads", False):
                    last_read = last_post

                population = member_counts.get(channel_id)
                if population is None:
                    # Fallback for MPIMs which calculate via member list length
                    if "members" in channel:
                        population = len(channel["members"])
                    else:
                        population = channel.get("num_members", 1)
                mass = population / total_pop

                category = "group" if channel.get("is_mpim") else "channel"

                chan = Channel(id=channel_id,
                               name=name,
                               topic=channel["topic"]["value"],
                               unread=1 if meta.get("has_unreads", False) else 0,
                               mentions=meta.get("mention_count", 0),
                               starred=channel_id in data["starred"],
                               last_read_at=last_read,
                               last_post_at=last_post,
                               mass=mass,
                               category=category
                               )
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

                last_read = float(meta.get("last_read", 0))
                last_post = float(meta.get("latest", im.get("latest", 0)))
                chan = Channel(id=channel_id,
                               name=user_name,
                               topic=f"Conversation with {user_name}",
                               unread=1 if meta.get("has_unreads", False) else 0,
                               mentions=meta.get("mention_count", 0),
                               starred=channel_id in data.get("starred", []),
                               last_read_at=last_read,
                               last_post_at=last_post,
                               mass=1/total_pop,
                               category="direct",
                               )
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

    async def _connection_loop(self):
        """WebSocket connection loop with automatic reconnection."""
        attempt = 0
        while self._running:
            try:
                await self.connect_ws()
                logger.info("Slack WebSocket connected")
                attempt = 0  # Reset on successful connection
                async for msg in self._ws:
                    # websockets library yields strings/bytes directly;
                    # aiohttp yields WSMessage objects with .data
                    data = msg.data if hasattr(msg, 'data') else msg
                    await self._inbox.put(data)
            except Exception as e:
                logger.error(f"Slack WS error: {e}")

            if self._running:
                delay = self._get_reconnect_delay(attempt)
                logger.info(f"Slack: reconnecting in {delay:.1f}s (attempt {attempt + 1})...")
                await asyncio.sleep(delay)
                attempt += 1

    async def events(self) -> AsyncGenerator[Event]:
        await self._login_event.wait()

        for event in await self.get_state_events():
            yield event

        ws_task = asyncio.create_task(self._connection_loop())
        try:
            while self._running:
                message = await self._inbox.get()
                try:
                    data = json.loads(message)
                    event = await self.handle_event(data)
                    if event:
                        self.get_state_events.cache_invalidate(self)
                        yield event
                except json.JSONDecodeError:
                    logger.debug(f"Received non-JSON message: {message}")
        finally:
            ws_task.cancel()


    @alru_cache
    async def get_state_events(self):
        """Get the current state event for the backend."""
        events = []
        await self.fetch_all_users()

        myself = self._users[self._user_id]
        info_event = self.create_event(
            event="self_info",
            user={
                "id": self._user_id,
                "name": username(myself),
                "color": "#" + myself.get("color", "cccccc")
            },
        channel_prefix="#"
        )

        channels = await self.get_subbed_channels()
        channel_list_event = self.create_event(event="channel_list",
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

        user_list_event = self.create_event(event="user_list", users=user_list)

        active_threads = await self.get_participated_threads()
        active_threads_event = self.create_event(event="thread_subscription_list",
                                                 thread_ids=active_threads)
        # logger.debug(active_threads_event)
        return info_event, user_list_event, channel_list_event, active_threads_event


    async def handle_event(self, json_data) -> Event | None:
        etype = json_data["type"]

        if etype == "message":
            logger.debug(pp(json_data))
            subtype = json_data.get("subtype")

            if subtype == "message_changed":
                new_json = json_data["message"]
                new_json["channel"] = json_data["channel"]
                raw_text = self._extract_message_body(new_json)
                body = self.emojis.replace_text(raw_text)
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
            elif subtype == "bot_message":
                if "user" in json_data:
                    asyncio.create_task(self.fetch_user(json_data["user"]))
                return await self.create_message_event_from_json(json_data)
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
                emoji=self.emojis.get_emoji(json_data["reaction"]),
                user_id=json_data["user"]
            )

        return None

    def _extract_message_body(self, blob) -> str:
        """Extract actionable text from a Slack message blob."""
        text = blob.get("text", "")
        sourced_from_attachments = False

        # 1. Fallback/Extraction for bot messages (GitHub, etc.) using attachments
        # We only do this if the main 'text' is empty.
        if not text and "attachments" in blob:
            parts = []
            for att in blob["attachments"]:
                att_parts = []

                # Pretext
                if "pretext" in att:
                    att_parts.append(att["pretext"])

                # Title
                if "title" in att:
                    if "title_link" in att:
                        att_parts.append(f"[{att['title']}]({att['title_link']})")
                    else:
                        att_parts.append(f"**{att['title']}**")

                # Text
                if "text" in att:
                    att_parts.append(att["text"])

                # Fields
                if "fields" in att:
                    for f in att["fields"]:
                        att_parts.append(f"**{f.get('title')}**: {f.get('value')}")

                if att_parts:
                    parts.append("\n".join(att_parts))
                elif "fallback" in att:
                    parts.append(att["fallback"])

            if parts:
                text = "\n\n".join(parts)

            # 2. SANITIZATION (Only for attachments/bots)
            # If the text came from a bot attachment, we sanitize it to prevent
            # ASCII tables from breaking the markdown renderer.
            # Fix Codecov artifacts: broken link syntax [text](<url>)
            text = re.sub(r"\]\(<([^>]+)>\)", r"](\1)", text)

            # Escape vertical bars to prevent accidental table rendering
            text = text.replace("|", "\\|")

            # Escape characters that create headers or lists at the start of lines.
            # # -> ATX Headers
            # =, - -> Setext Headers (underlines) and Horizontal Rules
            # + -> Lists
            text = re.sub(r"(^|\n)(#+|=+|-+|\++)", r"\1\\\2", text)

        # 3. UNIVERSAL CLEANUP (Applies to everyone)
        # Convert Slack-style links <url|text> -> [text](url)
        text = re.sub(r"<([^|]+)\|([^>]+)>", r"[\2](\1)", text)
        # Convert bare links <url> -> url
        text = re.sub(r"<(https?://[^>]+)>", r"\1", text)

        return text


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

            text = self._extract_message_body(blob)

            body = self.emojis.replace_text(text)
            body = await self.replace_mentions_in_text(body)

            timestamp = float(blob["ts"])
            dt = datetime.fromtimestamp(timestamp)
            timestamp = int(timestamp * 1000)
            client_id = blob.get("client_msg_id")
            message = dict(body=body,
                           author=author,
                           id=message_id,
                           client_id=client_id,
                           timestamp=timestamp,
                           ts_date=dt.date().isoformat(),
                           ts_time=dt.time().isoformat())

            if "thread_ts" in blob and blob["thread_ts"] != blob["ts"]:
                message["thread_id"] = blob["thread_ts"]

            if "reactions" in blob:
                reactions = {}
                for rx in blob["reactions"]:
                    emoji = self.emojis.get_emoji(rx["name"])
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
    async def get_participated_threads(self) -> list[str]:
        """Fetch thread IDs using the internal 'Threads' view endpoint.
        This replaces the unavailable users.threads endpoint.
        """
        try:
            # Use the internal endpoint found in the HAR trace.
            # 'priorityMode' generally corresponds to the "Threads" sidebar logic.
            # 'org_wide_aware' is required for modern grid workspaces.
            data = await self._post(
                "subscriptions.thread.getView",
                fetch_threads_state="true",
                limit=50,
                org_wide_aware="true",
                priorityMode="all",
                _x_reason="fetch-threads-via-refresh",
                _x_mode="online",
                _x_sonic="true",
                _x_app_name="cient",
            )

            threads = []
            if data.get("ok"):
                for thread in data.get("threads", []):
                    # 1. Identify IDs
                    tid = thread.get("root_msg", {}).get("ts")
                    cid = thread.get("channel_id") # Often at root
                    if not cid and "root_msg" in thread:
                         cid = thread["root_msg"].get("channel")

                    if not tid or not cid:
                        continue

                    # 2. TIMESTAMP BASED UNREAD LOGIC
                    # The snippet shows these are strings!
                    last_read = float(thread.get("last_read", 0))
                    latest_reply = float(thread.get("latest_reply", 0))

                    # Slack logic: It's unread if the newest reply is newer than your last read cursor
                    is_unread = latest_reply > last_read

                    threads.append({
                        "id": tid,
                        "channel_id": cid,
                        "unread": is_unread
                    })

            logger.debug(f"Hydrated {len(threads)} participated threads from Slack")
            return threads

        except Exception:
            logger.exception("Failed to fetch Slack threads")
            return []

    @override
    async def close(self):
        self._running = False
        if self._session:
            await self._session.close()
        if self._ws:
            await self._ws.close()

def username(user_info):
    return user_info["profile"].get("display_name") or user_info.get("real_name") or user_info.get("name")
