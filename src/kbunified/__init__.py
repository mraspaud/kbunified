# SPDX-FileCopyrightText: 2025-present Martin Raspaud <martin.raspaud@smhi.se>
#
# SPDX-License-Identifier: GPL-3.0+

import argparse
import asyncio
import logging
import signal
from collections.abc import AsyncGenerator, Iterable
from pathlib import Path
from tomllib import load

from kbunified.backends import chat_backends_from_config
from kbunified.backends.interface import ChatBackend, Event
from kbunified.ui import UIAPI

logger = logging.getLogger("main")

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

async def main(args=None):
    """Main entry point with multiple backends.

    - Starts login for all backends concurrently without waiting for all to complete.
    - Immediately starts serving events from those backends that are already connected.
    - Uses a dummy UI to echo an event back as a command.
    - Returns the echoed command.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("config_file", type=Path)
    opts = parser.parse_args(args)
    with opts.config_file.open("rb") as fp:
        config = load(fp)
    # Create multiple backends.
    backends = chat_backends_from_config(
            **config
        # service1={"backend": "dummy", "name": "Dummy1"},
        # service2={"backend": "dummy", "name": "Dummy2"}
    )

    # Start login tasks concurrently but do not wait for all of them here.
    login_tasks = [asyncio.create_task(backend.login())
                   for backend in backends.values()]

    # Start the UI API servers using merged events from all backends.
    logger.debug("Start ui api")
    ui_api = UIAPI()

    async def ui_close():
        await ui_api.close()
        asyncio.get_event_loop().stop()

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(ui_close()))

    merged_gen = merge_events(backends.values())
    await ui_api.send_events_to_ui(merged_gen)
    await ui_api.start_command_reception_server()
    try:
        async for cmd in ui_api.receive_commands_from_ui():
            if cmd.get("service_id") not in backends:
                logger.debug("Don't know service, ignoring command.")
            if cmd["command"] == "post_message":
                logger.debug(f"Sending message to {cmd['service_id']}")
                await backends[cmd["service_id"]].post_message(cmd["channel_id"], cmd["body"])
            if cmd["command"] == "switch_channel":
                logger.debug(f"Switching channel on {cmd['service_id']}")
                await backends[cmd["service_id"]].switch_channel(cmd["channel_id"])
    finally:
        # Shut down: signal backends to close and clean up the UI API.
        for backend in backends.values():
            backend.close()
        await ui_api.close()

    # Optionally, wait for all login tasks to complete (or handle them as needed).
    # await asyncio.gather(*login_tasks, return_exceptions=True)
    # await ui_client_task

    # return cmd

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(main())

