# src/kbunified/ui/__init__.py
"""UI API (aiohttp Implementation)."""

import asyncio
import json
import logging
from asyncio import Queue
from collections.abc import AsyncGenerator, Iterable
from dataclasses import asdict, is_dataclass
from pathlib import Path

from aiohttp import WSMsgType, web

from kbunified.backends.interface import Command, Event

logger = logging.getLogger("ui")

class UIAPI:
    """The UI API class using aiohttp (HTTP + WebSocket)."""

    def __init__(self, static_dir: str = None, port: int = 4722):
        self.port = port
        self.static_dir = Path(static_dir) if static_dir else None

        self._command_q: Queue[Command] = Queue()
        self.connected_clients = set()

        self._channel_lists = []
        self._handshakes = []
        self._user_lists = []

        self._app = web.Application()
        self._setup_routes()
        self._runner = None
        self._site = None

    def _setup_routes(self):
        # API Route
        self._app.router.add_get("/ws", self._websocket_handler)

        # Static Routes (Appliance Mode)
        if self.static_dir and self.static_dir.exists():
            logger.info(f"Serving static files from {self.static_dir}")
            # 1. Serve index.html at root "/"
            self._app.router.add_get("/", self._index_handler)
            # 2. Serve assets (JS/CSS)
            self._app.router.add_static("/", self.static_dir)
        else:
            logger.warning("No static directory provided. Serving API only.")

    async def _index_handler(self, request):
        return web.FileResponse(self.static_dir / "index.html")

    async def _websocket_handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        self.connected_clients.add(ws)
        logger.debug(f"UI Client connected (Total: {len(self.connected_clients)})")

        # 1. Send Cached State
        try:
            for handshake in self._handshakes:
                await ws.send_str(handshake)
            for chlist in self._channel_lists:
                await ws.send_str(chlist)
            for ulist in self._user_lists:
                await ws.send_str(ulist)

            # 2. Listen Loop
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        if isinstance(data, dict):
                            await self._command_q.put(data)
                    except json.JSONDecodeError:
                        logger.error("Failed to decode JSON from UI")
                elif msg.type == WSMsgType.ERROR:
                    logger.error(f"WS connection closed with exception {ws.exception()}")
        finally:
            self.connected_clients.remove(ws)
            logger.debug("UI Client disconnected")

        return ws

    async def start(self):
        """Start the aiohttp server (Non-blocking)."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await self._site.start()
        logger.info(f"Solaria Server running on http://127.0.0.1:{self.port}")

    async def send_events_to_ui(self, events: AsyncGenerator[Event, None]):
        """Consume backend events eagerly and broadcast."""
        logger.debug("Event broadcaster started")
        async for item in events:
            await self.broadcast_event(item)

    async def send_events_to_ui_list(self, events: Iterable[Event]):
        """Helper for testing: send a list of events immediately."""
        for item in events:
            await self.broadcast_event(item)

    async def broadcast_event(self, item: Event):
        """Serialize and send an event to all connected clients."""
        try:
            # Serialize
            if is_dataclass(item):
                data = asdict(item)
            else:
                data = item

            payload = json.dumps(data)

            # Cache Critical State
            evt_type = data.get("event")
            if evt_type == "self_info":
                self._handshakes.append(payload)
            elif evt_type == "channel_list":
                self._channel_lists.append(payload)
            elif evt_type == "user_list":
                self._user_lists.append(payload)

            # Broadcast
            if self.connected_clients:
                # Copy set to avoid modification during iteration
                for ws in list(self.connected_clients):
                    try:
                        await ws.send_str(payload)
                    except Exception:
                        pass # Client likely disconnected, cleanup handled by handler
        except Exception as e:
            logger.error(f"Error broadcasting event: {e}")

    async def receive_commands_from_ui(self) -> AsyncGenerator[Command, None]:
        while True:
            command = await self._command_q.get()
            yield command
            self._command_q.task_done()

    async def close(self):
        logger.debug("Closing UI API")
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
