"""Backend for Rocket.Chat."""

import asyncio
import logging
import ssl
from contextlib import suppress
from datetime import datetime
from urllib.parse import urlparse

import truststore
from rocketchat_async import RocketChat

from kbunified.backends.interface import ChatBackend, Event, create_event

logger = logging.getLogger("rocket.chat_backend")

class RocketChatBackend(ChatBackend):
    """The Rocket.Chat backend."""

    def __init__(self, service_id, name, service_url, userid, token):
        self._rc = RocketChat()
        self._service_id = service_id
        self.name = name
        self._userid = userid
        self._token = token
        self._service_url = service_url
        self._logged_in: bool = False
        self._login_event: asyncio.Event = asyncio.Event()
        self._messages = asyncio.Queue()
        self._running = True
        self._base_url = urlparse(service_url).hostname
        self.ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


    def is_logged_in(self):
        """Return logged in status."""
        return self._logged_in

    async def login(self):
        """Log in."""
        await self._rc.resume(self._service_url, self._userid, self._token, ssl=self.ssl_ctx)
        self._logged_in = True
        self._login_event.set()
        logger.debug(f"logged in {self.name}")

    async def post_message(self, channel_id, message):
        """Post a message"""
        logger.debug(f"Posting {message} to {channel_id}")
        await self._rc.send_message(message, channel_id)

    def get_subbed_channels(self):
        """Get the list of channels."""
        return dict(dummy_channel_1=dict(name="Dummy Channel #1"))

    async def events(self):
        """Event generator."""
        logger.debug("Rocket.Chat ready to roll")
        _ = await self._login_event.wait()
        channels = []
        try:
            for channel_dict in await self._rc.get_channels_raw():
                logger.debug(channel_dict)
                channel_type = channel_dict["t"]
                channel_id = channel_dict["_id"]
                if channel_type == "c":
                    chan = dict(name=channel_dict["fname"],
                                id=channel_id)
                elif channel_type == "d":
                    chan = dict(name=str(channel_dict["usernames"]),
                                id=channel_id)
                else:
                    raise NotImplementedError(f"Don't know how to handle channel type {channel_type}")
                channels.append(chan)
                await self._rc.subscribe_to_channel_messages_raw(channel_id, self.message_handler)
                with suppress(KeyError):
                    self.message_handler(channel_dict["lastMessage"])
        except:
            logger.exception("Error in channel listing")

        channel_list = create_event(event="channel_list",
                                    service=dict(name=self.name, id=self._service_id),
                                    channels=channels)
        yield channel_list

        while self._running:
            yield await self._messages.get()

    def close(self):
        """Shut down the backend."""
        self._running = False

    def create_message_from_blob(self, blob) -> Event:
        """Create a message from a blob dict."""
        channel_id = blob["rid"]
        user_info = blob["u"]
        author = dict(id=user_info["_id"], username=user_info["username"],
                      display_name=user_info["username"], full_name=user_info["name"])
        logger.debug(blob["u"])
        try:
            body = blob["msg"]
        except KeyError:
            files = [f'{att["description"]}: [{att["title"]}](https://{self._base_url}{att["title_link"]})'
                     for att in blob["attachments"]]
            body = "\n".join(files)

        ts = blob_to_time(blob["ts"]["$date"])

        message = dict(body=body,
                       author=author,
                       id=blob["_id"],
                       ts_date=ts.date().isoformat(),
                       ts_time=ts.time().isoformat())

        with suppress(KeyError):
            message["thread_id"] = blob["tmid"]
        with suppress(KeyError):
            blob_date = blob["editedAt"]["$date"]
            edt = blob_to_time(blob_date)
            message["edit_date"] = edt.date().isoformat()
            message["edit_time"] = edt.time().isoformat()
        with suppress(KeyError):
            message["reactions"] = {k.strip(":"):v["usernames"] for k, v in blob["reactions"].items()}

        event = create_event(event="message",
                             channel_id=channel_id,
                             service=dict(name=self.name, id=self._service_id),
                             message=message)

        return event

    def message_handler(self, blob):
        """Handle incomming message."""
        logger.debug(blob)
        try:
            event = self.create_message_from_blob(blob)
            self._messages.put_nowait(event)
        except:
            logger.exception("message hanler crashed")
            raise


def blob_to_time(blob_date):
    timestamp, milliseconds = divmod(blob_date, 1000)
    edt = datetime.fromtimestamp(timestamp)
    edt.replace(microsecond=milliseconds*1000)
    return edt




