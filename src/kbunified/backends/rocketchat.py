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

from kbunified.backends.interface import Channel, ChatBackend, Event, create_event
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
        self._ws = None

        self._users = {}
        self._channels = {}
        self._room_types = {}

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
            heartbeat=30
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
                    try:
                        dt = datetime.fromisoformat(sub["ls"].replace("Z", "+00:00"))
                        last_read = dt.timestamp()
                    except: pass

                last_post = 0.0
                if "lm" in sub and isinstance(sub["lm"], str):
                    try:
                        dt = datetime.fromisoformat(sub["lm"].replace("Z", "+00:00"))
                        last_post = dt.timestamp()
                    except: pass

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
        await self.fetch_all_users()

        yield self.create_event(event="self_info", user={
            "id": self._user_id,
            "name": self._username,
            "color": str_to_color(self._user_id)
        })

        yield self.create_event(event="user_list", users=list(self._users.values()))

        channels = await self.get_subbed_channels()
        yield self.create_event(event="channel_list", channels=[asdict(c) for c in channels])

        ws_task = asyncio.create_task(self._connection_loop())

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
                        yield event
        finally:
            ws_task.cancel()

    async def _connection_loop(self):
        while self._running:
            try:
                await self.connect_ws()
                async for msg in self._ws:
                    if msg.type == WSMsgType.TEXT:
                        data = json.loads(msg.data)
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

        ts = int(datetime.fromisoformat(m["ts"].replace("Z", "+00:00")).timestamp() * 1000)
        dt = datetime.fromtimestamp(ts/1000)

        # Display Name Resolution - ROBUST
        display_name = author.get("real_name") or author.get("name") or "Unknown"

        msg = {
            "id": m["_id"],
            "body": m.get("msg", ""),
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

    @override
    async def post_message(self, channel_id, message_text):
        await self._post("chat.postMessage", {"roomId": channel_id, "text": message_text})

    @override
    async def post_reply(self, channel_id, thread_id, message_text):
        await self._post("chat.postMessage", {
            "roomId": channel_id,
            "text": message_text,
            "tmid": thread_id
        })

    @override
    async def update_message(self, channel_id, message_id, new_text):
        await self._post("chat.update", {"roomId": channel_id, "msgId": message_id, "text": new_text})

    @override
    async def delete_message(self, channel_id, message_id):
        await self._post("chat.delete", {"roomId": channel_id, "msgId": message_id})

    @override
    async def send_reaction(self, channel_id, message_id, reaction):
        emoji = reaction.replace(":", "")
        await self._post("chat.react", {"messageId": message_id, "emoji": emoji})

    @override
    async def remove_reaction(self, channel_id, message_id, reaction):
        emoji = reaction.replace(":", "")
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
                pass

    @override
    async def is_logged_in(self) -> bool:
        return self._auth_token is not None

    @override
    async def close(self):
        self._running = False
        await self._session.close()

    def create_event(self, event, **fields) -> Event:
        return create_event(event=event, service=dict(name=self.name, id=self._service_id), **fields)
