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

from kbunified.backends.interface import ChatBackend, Command, Event

logger = logging.getLogger("ui")



async def merge_events(backends: Iterable[ChatBackend]) -> AsyncGenerator[Event]:
    """Merge events from multiple backends into a single async generator."""
    queue: asyncio.Queue[Event] = asyncio.Queue()
    async def pump_events(backend: ChatBackend):
        async for event in backend.events():
            await queue.put(event)
    tasks = [asyncio.create_task(pump_events(backend)) for backend in backends]
    try:
        while True:
            event = await queue.get()
            yield event
    finally:
        for t in tasks:
            t.cancel()


class UIAPI:
    """The UI API class using aiohttp (HTTP + WebSocket)."""

    def __init__(self, backends, static_dir: str = None, port: int = 4722):
        self.port = port
        self.static_dir = Path(static_dir) if static_dir else None
        self._backends = backends

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
            await self.push_state(ws)
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

    async def push_state(self, ws):
        for backend in self._backends.values():
            # FIXME: why can't we do a async for here? why is yield not valid in certain versions of get_state_events
            for event in await backend.get_state_events():
                payload = _serialize_event(event)
                await ws.send_str(payload)


    async def start(self):
        """Start the aiohttp server (Non-blocking)."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await self._site.start()
        logger.info(f"Solaria Server running on http://127.0.0.1:{self.port}")

    async def send_backend_events_to_ui(self):
        merged_gen = merge_events(self._backends.values())
        async for item in merged_gen:
            await self.broadcast_event(item)

    async def broadcast_event(self, item: Event):
        """Serialize and send an event to all connected clients."""
        try:
            # Serialize
            payload = _serialize_event(item)

            # Cache Critical State
            if is_dataclass(item):
                data = asdict(item)
            else:
                data = item
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

def _serialize_event(event):
    if is_dataclass(event):
        data = asdict(event)
    else:
        data = event

    return json.dumps(data)
