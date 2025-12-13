"""Backend for Rocket.Chat (DDP/WebSocket + REST)."""

import asyncio
import colorsys
import hashlib
import json
import logging
import ssl
from collections.abc import AsyncGenerator
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import override

import aiohttp
import truststore
from aiohttp import WSMsgType
from async_lru import alru_cache

from kbunified.backends.interface import Channel, ChannelID, ChatBackend, Event, create_event
from kbunified.utils.emoji import EmojiManager
from kbunified.utils.rocketchat_auth import get_rocketchat_credentials

logger = logging.getLogger("rocketchat_backend")

def str_to_color(str_in):
    """Converts a UUID/String into a well-contrasted HTML color string."""
    fixed_saturation = 0.7
    fixed_value = 0.5
    try:
        md5 = hashlib.md5(str_in.encode("utf-8"))
        md5_int = int(md5.hexdigest(), 16)
        hue = (md5_int % 360) / 360.0
    except ValueError:
        return "#cccccc"

    rgb_float = colorsys.hsv_to_rgb(hue, fixed_value, fixed_saturation)
    rgb_int = tuple(int(c * 255) for c in rgb_float)
    return "#{:02x}{:02x}{:02x}".format(*rgb_int)

def _get_attachment_path(file_id, file_name):
    base = Path.home() / ".cache" / "kb-solaria" / "attachments"
    base.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c for c in file_name if c.isalnum() or c in "._- ")
    return base / f"{file_id}_{safe_name}"


def _get_ts_from_rc_blob(blob):
    try:
        return int(datetime.fromisoformat(blob.replace("Z", "+00:00")).timestamp() * 1000)
    except AttributeError:
        return blob["$date"]


class RocketChatBackend(ChatBackend):
    """The Rocket.Chat backend."""

    def __init__(self, service_id: str, name: str, domain: str, user: str, password: str = None, token: str = None):
        self._service_id = service_id
        self.name = name

        # Normalize domain
        if domain.startswith("http"):
            self._http_base = domain
            self._ws_url = domain.replace("http", "ws") + "/websocket"
        else:
            self._http_base = f"https://{domain}"
            self._ws_url = f"wss://{domain}/websocket"

        self._username = user
        self._password = password
        self._auth_token = token
        self._user_id = None

        # Auto-Extraction
        if not self._auth_token and not self._password:
            logger.info("No credentials provided. Attempting to extract from Firefox...")
            uid, extracted_token, extracted_username = get_rocketchat_credentials(domain)
            if uid and extracted_token:
                self._user_id = uid
                self._auth_token = extracted_token
                if extracted_username and not self._username:
                    self._username = extracted_username

        self._session = aiohttp.ClientSession()
        self.ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

        self._running = True
        self._login_event = asyncio.Event()
        self._inbox = asyncio.Queue()
        self._ws = []

        self._users = {}
        self._channels = {}
        self._room_types = {}
        self.emojis = EmojiManager()

    async def _ddp_heartbeat(self):
        """Sends a DDP-level ping every 30 seconds to keep the session alive."""
        while self._running:
            try:
                await asyncio.sleep(30)
                if self._ws and not self._ws.closed:
                    # DDP Ping (Server responds with {"msg": "pong"})
                    await self._send_ws({"msg": "ping"})
                    logger.debug("Sent DDP Ping")
            except Exception:
                # If we fail to ping, the connection loop will handle the restart
                pass

    @override
    async def login(self):
        """Perform REST login to get Auth Token if not provided."""
        if not self._auth_token and self._password:
            payload = {"user": self._username, "password": self._password}
            try:
                data = await self._post("login", payload)
                if data.get("status") == "success":
                    self._auth_token = data["data"]["authToken"]
                    self._user_id = data["data"]["userId"]
                    logger.debug(f"Rocket.Chat REST login successful: {self._user_id}")
                else:
                    raise PermissionError(f"Login failed: {data.get('message')}")
            except Exception as e:
                logger.error(f"Rocket.Chat login error: {e}")
                raise

        if self._auth_token and self._user_id:
             if not self._username:
                 try:
                     me = await self._get("me")
                     if me.get("success") is not False:
                          self._username = me.get("username", self._username)
                 except Exception as e:
                     logger.warning(f"Failed to verify session/fetch username: {e}")

        self._login_event.set()

    # --- HTTP HELPERS ---

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self._auth_token and self._user_id:
            h["X-Auth-Token"] = self._auth_token
            h["X-User-Id"] = self._user_id
        return h

    async def _request(self, method, endpoint, **kwargs):
        url = f"{self._http_base}/api/v1/{endpoint}"
        async with self._session.request(
            method,
            url,
            headers=self._headers(),
            ssl=self.ssl_ctx,
            **kwargs
        ) as resp:
            try:
                data = await resp.json()
            except:
                data = {}

            if resp.status >= 400:
                logger.error(f"RC Req Failed {endpoint}: {data.get('error', resp.status)}")
            return data

    async def _get(self, endpoint, **params):
        return await self._request("GET", endpoint, params=params)

    async def _post(self, endpoint, payload):
        return await self._request("POST", endpoint, json=payload)

    async def _download_file(self, url, path):
        if not url.startswith("http"):
            url = f"{self._http_base}{url}"
        try:
            async with self._session.get(url, headers=self._headers(), ssl=self.ssl_ctx) as resp:
                if resp.status == 200:
                    with path.open("wb") as fd:
                        fd.write(await resp.read())
                    logger.debug(f"Downloaded {path}")
        except Exception:
            logger.exception(f"Failed to download file to {path}")

    # --- WEBSOCKET & DDP ---

    async def connect_ws(self):
        logger.debug("Connecting to Rocket.Chat WebSocket...")
        self._ws = await self._session.ws_connect(
            self._ws_url,
            ssl=self.ssl_ctx,
            # heartbeat=30
        )
        await self._send_ws({"msg": "connect", "version": "1", "support": ["1"]})

        login_msg = {
            "msg": "method",
            "method": "login",
            "id": "login_call",
            "params": [{"resume": self._auth_token}]
        }
        await self._send_ws(login_msg)

        await self._sub_ws("stream-room-messages", "__my_messages__", False)
        await self._sub_ws("stream-notify-user", f"{self._user_id}/notification", False)

        logger.debug("Rocket.Chat WS Ready")
        return self._ws

    async def _send_ws(self, msg):
        if self._ws and not self._ws.closed:
            # logger.debug(f"RC Sent through ws {datetime.now()}:\n" + str(msg))
            await self._ws.send_json(msg)

    async def _sub_ws(self, name, *params):
        msg = {
            "msg": "sub",
            "id": f"sub_{name}_{params[0]}",
            "name": name,
            "params": list(params)
        }
        await self._send_ws(msg)

    # --- INTERFACE METHODS ---

    @override
    async def get_subbed_channels(self):
        channels = []
        try:
            tasks = [
                self._get("subscriptions.get"),
                self._get("channels.list", count=0),
                self._get("groups.list", count=0)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            subs_data = results[0] if not isinstance(results[0], Exception) else {}
            channels_data = results[1] if not isinstance(results[1], Exception) else {}
            groups_data = results[2] if not isinstance(results[2], Exception) else {}

            counts = {}
            for c in channels_data.get("channels", []):
                counts[c["_id"]] = c.get("usersCount", 0)
            for g in groups_data.get("groups", []):
                counts[g["_id"]] = g.get("usersCount", 0)

            total_pop = max(1, len(self._users))

            for sub in subs_data.get("update", []):
                if not sub.get("open", True): continue

                cid = sub["rid"]
                name = sub["name"]
                rtype = sub["t"] # c, p, d, l

                self._room_types[cid] = rtype

                member_count = 2 if rtype == "d" else counts.get(cid, 1)
                mass = member_count / total_pop

                last_read = 0.0
                if "ls" in sub and isinstance(sub["ls"], str):
                    last_read = _get_ts_from_rc_blob(sub["ls"]) / 1000

                last_post = 0.0
                if "lm" in sub and isinstance(sub["lm"], str):
                    last_post = _get_ts_from_rc_blob(sub["lm"]) / 1000

                chan = Channel(
                    id=cid,
                    name=name,
                    topic=sub.get("topic", ""),
                    unread=sub.get("unread", 0) > 0,
                    mentions=sub.get("userMentions", 0),
                    starred=sub.get("f", False),
                    last_read_at=last_read,
                    last_post_at=last_post,
                    mass=mass
                )
                self._channels[cid] = chan
                channels.append(chan)

        except Exception:
            logger.exception("Failed fetching RC channels")
        return channels

    async def fetch_all_users(self):
        try:
            logger.debug("Fetching RC users...")
            data = await self._get("users.list", count=200)
            for u in data.get("users", []):
                self._users[u["_id"]] = {
                    "id": u["_id"],
                    "name": u["username"],
                    "real_name": u.get("name"),
                    "color": str_to_color(u["_id"])
                }
            logger.debug(f"Fetched {len(self._users)} RC users")
        except Exception:
            logger.exception("User fetch failed")

    @override
    async def events(self) -> AsyncGenerator[Event]:
        await self._login_event.wait()

        for event in await self.get_state_events():
            yield event

        ws_task = asyncio.create_task(self._connection_loop())
        # ka_task = asyncio.create_task(self._ddp_heartbeat())

        try:
            while self._running:
                raw_msg = await self._inbox.get()

                try:
                    if isinstance(raw_msg, str):
                        data = json.loads(raw_msg)
                    else:
                        data = raw_msg
                except json.JSONDecodeError:
                    continue

                if "event" in data and "message" in data:
                    yield data
                else:
                    event = await self.handle_ws_message(data)
                    if event:
                        self.get_state_events.cache_invalidate(self)
                        yield event
        except Exception:
            logger.exception("oh no...")
        finally:
            ws_task.cancel()
            # ka_task.cancel()

    @alru_cache
    async def get_state_events(self):
        """Get the current state event for the backend."""
        # ws_task = asyncio.create_task(self.fetch_custom_emojis())
        await self.fetch_all_users()

        info_event = self.create_event(event="self_info", user={
            "id": self._user_id,
            "name": self._username,
            "color": str_to_color(self._user_id)
        })
        user_list_event = self.create_event(event="user_list", users=list(self._users.values()))

        channels = await self.get_subbed_channels()
        channel_list_event = self.create_event(event="channel_list", channels=[asdict(c) for c in channels])
        return info_event, user_list_event, channel_list_event
    #
    #     active_threads = await self.get_participated_threads()
    #     active_threads_event = self.create_event(event="thread_subscription_list",
    #                                              thread_ids=active_threads)
    #     return info_event, user_list_event, channel_list_event, active_threads_event

    async def _connection_loop(self):
        while self._running:
            try:
                await self.connect_ws()
                async for msg in self._ws:
                    if msg.type == WSMsgType.TEXT:
                        data = json.loads(msg.data)

                        # logger.debug(f"Received from ws at {datetime.now()}:\n" + str(data))
                        if data.get("msg") == "ping":
                            await self._send_ws({"msg": "pong"})
                            continue
                        await self._inbox.put(data)
                    elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
            except Exception as e:
                logger.error(f"RC WS Error: {e}")
                await asyncio.sleep(5)

    async def handle_ws_message(self, data):
        msg_type = data.get("msg")
        if msg_type == "changed" and data.get("collection") == "stream-room-messages":
            args = data.get("fields", {}).get("args", [])
            if not args: return None
            return self._parse_message(args[0])
        return None

    def _parse_message(self, m):
        rid = m["rid"]
        uid = m["u"]["_id"]

        # Author Resolution
        author = self._users.get(uid)

        # Fallback if author not found
        if not author:
            # Check if 'u' has keys we need
            username_val = m["u"].get("username", "Unknown")
            # If system message, username might be 'rocket.cat' or similar
            if "alias" in m:
                username_val = m["alias"]

            author = {
                "id": uid,
                "name": username_val,
                "real_name": m["u"].get("name"),
                "color": str_to_color(uid)
            }
        ts = _get_ts_from_rc_blob(m["ts"])
        dt = datetime.fromtimestamp(ts/1000)

        # Display Name Resolution - ROBUST
        display_name = author.get("real_name") or author.get("name") or "Unknown"

        rc_type = self._room_types.get(rid, "c")
        category = "channel"
        if rc_type == "d":
            category = "direct"
        elif rc_type == "p":
            category = "group"

        # 2. Resolve Client ID (Optimism)
        client_id = None
        if uid == self._user_id:
            client_id = m["_id"]

        msg = {
            "id": m["_id"],
            "body": self.emojis.replace_text(m.get("msg", "")),
            "client_id": client_id,
            "channel_category": category,
            "author": {
                "id": author["id"],
                "name": author["name"],
                "display_name": display_name, # <--- ENSURE THIS IS SET
                "color": author["color"]
            },
            "timestamp": ts,
            "ts_date": dt.date().isoformat(),
            "ts_time": dt.time().isoformat()
        }

        if "tmid" in m:
            msg["thread_id"] = m["tmid"]

        if "tcount" in m:
            msg["replies"] = {"count": m["tcount"]}

        # --- NEW: Reactions Support ---
        if "reactions" in m:
            reactions = {}
            for emoji, details in m["reactions"].items():
                # RocketChat emojis might come as ':smile:', strip colons
                clean_emoji = self.emojis.get_emoji(emoji)
                # 'usernames' is a list of usernames, we might want IDs if possible,
                # but RC often sends usernames in this blob.
                # Ideally we map back to IDs, but for now usernames are often accepted by UI.
                # If your UI strictly needs IDs, we'd have to lookup via self._users.
                reactions[clean_emoji] = details.get("usernames", [])
            msg["reactions"] = reactions

        if "attachments" in m:
            atts = []
            for a in m["attachments"]:
                if "image_url" in a:
                    fname = a.get("title", "image.jpg")
                    fid = hashlib.md5(a["image_url"].encode()).hexdigest()
                    path = _get_attachment_path(fid, fname)
                    if not path.exists():
                        asyncio.create_task(self._download_file(a["image_url"], path))
                    atts.append({"id": fid, "name": fname, "path": str(path)})
            msg["attachments"] = atts

        return self.create_event(event="message", channel_id=rid, message=msg)


    # --- ACTIONS ---

    async def _call_method(self, method, *params):
        """Call a DDP method via WebSocket."""
        for _ in range(10):
            if self._ws and not self._ws.closed:
                break
            await asyncio.sleep(0.5)

        if not self._ws or self._ws.closed:
            logger.error(f"Failed to send {method}: WS disconnected")
            return

        msg_id = hashlib.md5(f"{method}{params}{datetime.now()}".encode()).hexdigest()
        msg = {
            "msg": "method",
            "method": method,
            "params": list(params),
            "id": msg_id
        }

        await self._send_ws(msg)

    # async def fetch_custom_emojis(self):
    #     try:
    #         data = await self._get("emoji-custom.list")
    #         if data.get("success"):
    #             base_url = self._http_base
    #             print(data)
    #             for em in data.get("emojis", []):
    #                 name = em["name"]
    #                 ext = em.get("extension", "png")
    #                 url = f"{base_url}/emoji-custom/{name}.{ext}"
    #
    #                 self.emojis.register_custom(name, url)
    #                 for alias in em.get("aliases", []):
    #                     self.emojis.register_custom(alias, url)
    #     except Exception:
    #         logger.exception("Failed to fetch RC custom emojis")

    @override
    async def post_message(self, channel_id: ChannelID, message_text: str, client_id: str | None = None) -> str | None:
        if not client_id:
            import uuid
            client_id = str(uuid.uuid4())

        message_obj = {
            "_id": client_id,
            "rid": channel_id,
            "msg": message_text
        }

        await self._call_method("sendMessage", message_obj)

        return client_id

    @override
    async def post_reply(self, channel_id: ChannelID, thread_id: str, message_text: str, client_id: str | None = None) -> str | None:
        if not client_id:
            import uuid
            client_id = str(uuid.uuid4())

        message_obj = {
            "_id": client_id,
            "rid": channel_id,
            "msg": message_text,
            "tmid": thread_id # Thread ID
        }

        await self._call_method("sendMessage", message_obj)
        return client_id

    @override
    async def update_message(self, channel_id, message_id, new_text):
        await self._post("chat.update", {"roomId": channel_id, "msgId": message_id, "text": new_text})

    @override
    async def delete_message(self, channel_id, message_id):
        await self._post("chat.delete", {"roomId": channel_id, "msgId": message_id})

    @override
    async def send_reaction(self, channel_id, message_id, reaction):
        emoji = self.emojis.get_shortcode(reaction)
        await self._post("chat.react", {"messageId": message_id, "emoji": emoji})

    @override
    async def remove_reaction(self, channel_id, message_id, reaction):
        emoji = self.emojis.get_shortcode(reaction)
        await self._post("chat.react", {"messageId": message_id, "emoji": emoji})

    @override
    async def switch_channel(self, channel_id, after=None):
        endpoint_candidates = []
        rtype = self._room_types.get(channel_id)

        if rtype == "c":
            endpoint_candidates.append("channels.history")
        elif rtype == "p":
            endpoint_candidates.append("groups.history")
        elif rtype == "d":
            endpoint_candidates.append("im.history")
        else:
            endpoint_candidates = ["channels.history", "im.history", "groups.history"]

        for endpoint in endpoint_candidates:
            try:
                res = await self._get(endpoint, roomId=channel_id, count=50)

                if res.get("success"):
                    messages = res.get("messages", [])
                    # Reverse to send oldest first
                    for m in reversed(messages):
                        event = self._parse_message(m) # Returns Dict
                        if event:
                            await self._inbox.put(event) # Put DICT, not JSON string
                    return
            except Exception:
                logger.exception("Something went wrong when switching channels")

    @override
    async def fetch_thread(self, channel_id: ChannelID, thread_id: str, after: str | None = None):
        """Fetch messages from a thread."""
        # Endpoint: /api/v1/chat.getThreadMessages
        # Rocket.Chat typically uses 'offset' rather than cursor IDs for this endpoint,
        # but we'll stick to a reasonable default count for parity.
        params = {"tmid": thread_id, "count": 100}

        # If strict pagination is needed later, 'after' logic would convert to 'offset' here.

        try:
            res = await self._get("chat.getThreadMessages", **params)

            if res.get("success"):
                messages = res.get("messages", [])

                # Rocket.Chat returns newest first.
                # We reverse to push oldest -> newest to the UI.
                for m in reversed(messages):
                    event = self._parse_message(m)
                    if event:
                        # Unlike Slack/Mattermost which sometimes push raw JSON strings,
                        # this backend's event loop handles pre-parsed dicts gracefully.
                        await self._inbox.put(event)
            else:
                logger.error(f"Failed to fetch thread {thread_id}: {res.get('error', 'Unknown error')}")

        except Exception:
            logger.exception("Something went wrong when fetching thread")

    @override
    async def is_logged_in(self) -> bool:
        return self._auth_token is not None

    @override
    async def mark_channel_read(self, channel_id: ChannelID, message_id: str):
        """Mark the channel as read."""
        # Rocket.Chat marks the entire subscription as read by Room ID
        await self._post("subscriptions.read", {"rid": channel_id})

    @override
    async def set_typing_status(self, channel_id: ChannelID):
        """Send typing indicator via WebSocket."""
        if self._ws and not self._ws.closed:
            # DDP method call
            msg = {
                "msg": "method",
                "method": "stream-notify-room",
                "params": [f"{channel_id}/typing", self._username, True],
                "id": f"typing_{int(datetime.now().timestamp())}"
            }
            await self._send_ws(msg)

    @override
    async def get_participated_threads(self) -> list[str]:
        """Hydrate thread context using Rocket.Chat's subscription metadata.
        We aggregate all 'tunread' lists to find threads the user needs to see.
        """
        try:
            # subscriptions.get returns ALL room memberships and their states
            data = await self._get("subscriptions.get")
            threads = set()

            for sub in data.get("update", []):
                # 1. Standard Unread Threads
                if "tunread" in sub and sub["tunread"]:
                    threads.update(sub["tunread"])

                # 2. Threads with Direct Mentions
                if "tunreadUser" in sub and sub["tunreadUser"]:
                    threads.update(sub["tunreadUser"])

                # 3. Threads with Group Mentions
                if "tunreadGroup" in sub and sub["tunreadGroup"]:
                    threads.update(sub["tunreadGroup"])

            logger.debug(f"Hydrated {len(threads)} active threads from Rocket.Chat")
            return list(threads)

        except Exception:
            logger.exception("Failed to fetch Rocket.Chat subscription threads")
            return []

    @override
    async def close(self):
        self._running = False
        await self._session.close()

    def create_event(self, event, **fields) -> Event:
        return create_event(event=event, service=dict(name=self.name, id=self._service_id), **fields)
