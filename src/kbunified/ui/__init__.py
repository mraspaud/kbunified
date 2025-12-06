"""UI API (WebSocket Implementation)."""

import asyncio
import json
import logging
from asyncio import Queue
from collections.abc import AsyncGenerator
from dataclasses import asdict, is_dataclass

# Check version for correct import
try:
    from websockets.asyncio.server import serve
except ImportError:
    from websockets import serve
import websockets

# Assuming these exist in your project structure
from kbunified.backends.interface import Command, Event

logger = logging.getLogger("ui")

class UIAPI:
    """The ui api class using WebSockets."""

    def __init__(self):
        self._server_task = None
        self._command_q: Queue[Command] = Queue()
        self.connected_clients = set()

        self._channel_lists = []
        self._handshakes = []
        self._user_lists = []

    async def _websocket_handler(self, websocket, path=None):
        """Handles a single WebSocket connection.
        """
        self.connected_clients.add(websocket)
        logger.debug("UI Client connected")

        for handshake in self._handshakes:
            await websocket.send(handshake)
        for chlist in self._channel_lists:
            await websocket.send(chlist)
        for ulist in self._user_lists:
            await websocket.send(ulist)

        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    if isinstance(data, dict):
                        command: Command = data
                        await self._command_q.put(command)
                except json.JSONDecodeError:
                    logger.error("Failed to decode JSON from UI")
        except Exception as e:
            logger.debug(f"Connection closed: {e}")
        finally:
            self.connected_clients.remove(websocket)
            logger.debug("UI Client disconnected")

    async def start_command_reception_server(self):
        logger.info("Starting WebSocket server on ws://127.0.0.1:8000")
        self._server = await serve(self._websocket_handler, "127.0.0.1", 8000)
        await self._server.wait_closed()

    async def send_events_to_ui(self, events: AsyncGenerator[Event, None]):
        """Consume backend events eagerly and broadcast.
        Does NOT block, so the backend can keep running logic (like starting WS listeners).
        """
        logger.debug("Event broadcaster started")

        async for item in events:
            try:
                # 1. Serialize
                if is_dataclass(item):
                    data = asdict(item)
                else:
                    data = item

                payload = json.dumps(data)

                # 2. Cache Critical State
                # We save this so late-joining clients aren't empty
                if data.get("event") == "self_info": # <--- 3. NEW: Cache the handshake
                        self._handshakes.append(payload)
                        logger.debug("Cached identity handshake")
                elif isinstance(data, dict) and data.get("event") == "channel_list":
                    self._channel_lists.append(payload)
                    logger.debug("Cached channel list")
                elif isinstance(data, dict) and data.get("event") == "user_list":
                    self._user_lists.append(payload)
                    logger.debug("Cached user list")

                # 3. Broadcast (if anyone is home)
                if self.connected_clients:
                    websockets.broadcast(self.connected_clients, payload)
                    logger.debug(f"Broadcasted event: {data.get('event', 'unknown')}")
                else:
                    # If no one is listening, we drop the message (except for the cache above).
                    # This keeps the backend loop moving.
                    logger.debug(f"Buffered/Dropped event (no clients): {data.get('event', 'unknown')}")

            except Exception as e:
                logger.error(f"Error broadcasting event: {e}")

    async def receive_commands_from_ui(self) -> AsyncGenerator[Command, None]:
        while True:
            command = await self._command_q.get()
            yield command
            self._command_q.task_done()

    async def close(self):
        logger.debug("Closing UI API")
        if hasattr(self, "_server") and self._server:
            self._server.close()
            await self._server.wait_closed()
