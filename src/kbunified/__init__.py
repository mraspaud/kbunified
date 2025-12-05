# SPDX-FileCopyrightText: 2025-present Martin Raspaud <martin.raspaud@smhi.se>
#
# SPDX-License-Identifier: GPL-3.0+

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import AsyncGenerator, Iterable
from pathlib import Path
from tomllib import load

from kbunified.backends import chat_backends_from_config
from kbunified.backends.interface import ChatBackend, Event
from kbunified.ui import UIAPI

# Configure logging to stdout
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

# Explicitly set websockets client to DEBUG
# This ensures you see the outbound handshake
logging.getLogger("websockets.client").setLevel(logging.DEBUG)
logging.getLogger("websockets.server").setLevel(logging.DEBUG)

# If urllib3/requests is used for auth, quiet it down to reduce noise
logging.getLogger("urllib3").setLevel(logging.WARNING)
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
    )

    # Start login tasks concurrently
    login_tasks = [asyncio.create_task(backend.login())
                   for backend in backends.values()]

    logger.debug("Start ui api")
    ui_api = UIAPI()

    async def ui_close():
        await ui_api.close()
        asyncio.get_event_loop().stop()

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(ui_close()))

    merged_gen = merge_events(backends.values())


    # await ui_api.start_command_reception_server()
    # 1. Start broadcasting events (Non-blocking now)

    sender_task = asyncio.create_task(ui_api.send_events_to_ui(merged_gen))

    # 2. Start the WebSocket Server (Non-blocking now)
    server_task = asyncio.create_task(ui_api.start_command_reception_server())

    try:
        # 3. This loop keeps the main program alive while processing commands
        async for cmd in ui_api.receive_commands_from_ui():
            try:
                if cmd.get("service_id") not in backends:
                    logger.debug("Don't know service, ignoring command.")
                    continue # Add continue to avoid crashing on unknown service

                service = backends[cmd["service_id"]] # Alias for readability

                if cmd["command"] == "post_message":
                    logger.debug(f"Sending message to {cmd['service_id']}")
                    await service.post_message(cmd["channel_id"], cmd["body"])

                elif cmd["command"] == "post_reply":
                    logger.debug(f"Sending reply to {cmd['service_id']}")
                    # Ensure your backend supports this method signature
                    await service.post_reply(cmd["channel_id"], cmd["thread_id"], cmd["body"])

                elif cmd["command"] == "switch_channel":
                    logger.debug(f"Switching channel on {cmd['service_id']}")
                    # Optional: Some backends don't need explicit switching
                    if hasattr(service, "switch_channel"):
                        await service.switch_channel(cmd["channel_id"], after=cmd.get("after"))

                elif cmd["command"] == "fetch_thread":
                    logger.debug(f"Fetch thread on {cmd['service_id']}")
                    if hasattr(service, "fetch_thread"):
                        await service.fetch_thread(cmd["channel_id"], cmd["thread_id"], after=cmd.get("after"))

                elif cmd["command"] == "message_update":
                    logger.debug(f"Updating message in {cmd['service_id']}")
                    # Ensure the backend supports updates
                    if hasattr(service, "update_message"):
                        await service.update_message(cmd["channel_id"], cmd["message_id"], cmd["body"])

                elif cmd["command"] == "message_delete":
                    logger.debug(f"Deleting message in {cmd['service_id']}")
                    if hasattr(service, "delete_message"):
                        await service.delete_message(cmd["channel_id"], cmd["message_id"])

                elif cmd["command"] == "react":
                    action = cmd["action"]  # Default to 'add' if unspecified
                    emoji = cmd.get("reaction")
                    logger.debug(f"{action.capitalize()}ing reaction '{emoji}' in {cmd['service_id']}")

                    if action == "add":
                        if hasattr(service, "send_reaction"):
                            await service.send_reaction(cmd["channel_id"], cmd["message_id"], emoji)
                    elif action == "remove":
                        if hasattr(service, "remove_reaction"):
                            await service.remove_reaction(cmd["channel_id"], cmd["message_id"], emoji)
            except Exception as e:
                logger.error(f"Error processing command {cmd.get('command')}: {e}")
    except Exception:
        logger.exception("Bad command or main loop error")
    finally:
        # Cleanup tasks when we exit
        sender_task.cancel()
        server_task.cancel()

        for backend in backends.values():
            await backend.close()
        await ui_api.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(main())

