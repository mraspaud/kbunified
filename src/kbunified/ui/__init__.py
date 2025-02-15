"""UI API."""

import json
import logging
from asyncio import Queue, start_unix_server
from collections.abc import AsyncGenerator
from contextlib import suppress
from functools import cache, partial
from pathlib import Path
from tempfile import gettempdir

from kbunified.backends.interface import Command, Event

logger = logging.getLogger("ui")


@cache
def get_send_socket():
    """Get the socket for sending."""
    return Path(gettempdir()) / "kb_events.sock"

@cache
def get_receive_socket():
    """Get the socket for sending."""
    return Path(gettempdir()) / "kb_commands.sock"

class UIAPI:
    """The ui api class."""

    def __init__(self):
        """Set up the ui api."""
        self._send_server = None
        self._receive_server = None
        self._command_q: Queue[Command] = Queue()

    async def send_events_to_ui(self, events: AsyncGenerator[Event]):
        """Send events to the ui."""
        sender_cb = partial(sender, events)
        self._send_server = await start_unix_server(sender_cb, get_send_socket())
        logger.debug("sender started")

    async def close(self):
        """Close servers."""
        logger.debug("Closing ui")
        with suppress(AttributeError):
            self._send_server.close()
            await self._send_server.wait_closed()
        with suppress(AttributeError):
            self._receive_server.close()
            await self._receive_server.wait_closed()

    async def start_command_reception_server(self):
        """Start the command-reception server."""
        self._receive_server = await start_unix_server(self._recv, get_receive_socket())
        logger.debug("command receiver started")

    async def _recv(self, reader, writer):
        """Receive one command through the socket and quit."""
        while True:
            line: bytes = await reader.readline()
            if not line:
                break
            logger.debug(f"got command: {line}")
            try:
                command: Command = json.loads(line.decode().strip())
                await self._command_q.put(command)
            except json.JSONDecodeError:
                pass
        writer.close()
        await writer.wait_closed()

    async def receive_commands_from_ui(self):
        """Receive commands from the ui."""
        while True:
            command = await self._command_q.get()
            yield command

async def sender(events: AsyncGenerator[Event], reader, writer):
    """Send events to the ui."""
    del reader
    async for item in events:
        logger.debug(f"Sending {item}")
        writer.write(json.dumps(item).encode() + b"\n")
        await writer.drain()
    writer.close()
    await writer.wait_closed()
