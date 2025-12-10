# SPDX-FileCopyrightText: 2025-present Martin Raspaud <martin.raspaud@smhi.se>
#
# SPDX-License-Identifier: Apache-2.0

import argparse
import asyncio
import logging
import os
import shutil
import signal
import subprocess
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

# AIOHTTP Logging (Less verbose than websockets, but good to have)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

# If urllib3/requests is used for auth, quiet it down to reduce noise
logging.getLogger("urllib3").setLevel(logging.WARNING)
logger = logging.getLogger("main")


async def main(args=None):
    """Main entry point with multiple backends."""
    parser = argparse.ArgumentParser()
    parser.add_argument("config_file", type=Path)
    parser.add_argument("--static-dir", type=Path, default=None, help="Path to static frontend files (dist)")
    opts = parser.parse_args(args)

    with opts.config_file.open("rb") as fp:
        config = load(fp)

    # Create multiple backends.
    backends = chat_backends_from_config(**config)

    # Start login tasks concurrently
    login_tasks = [asyncio.create_task(backend.login())
                   for backend in backends.values()]

    logger.debug("Starting UI API")
    # Initialize UI with the static directory (if provided)
    ui_api = UIAPI(backends, static_dir=opts.static_dir)

    async def ui_close():
        await ui_api.close()
        asyncio.get_event_loop().stop()

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(ui_close()))

    # 1. Start broadcasting events (Non-blocking)
    sender_task = asyncio.create_task(ui_api.send_backend_events_to_ui())

    # 2. Start the Server (Non-blocking initialization)
    # The new aiohttp runner starts immediately and runs in the background
    await ui_api.start()

    try:
        # 3. This loop keeps the main program alive while processing commands
        async for cmd in ui_api.receive_commands_from_ui():
            try:
                if cmd["command"] == "open_path":
                    path = cmd["path"]
                    logger.debug(f"Opening path: {path}")
                    try:
                        opener = None
                        if sys.platform == "win32":
                            os.startfile(path)
                        elif sys.platform == "darwin":
                            opener = "open"
                        else:
                            opener = "xdg-open"

                        if opener:
                            if shutil.which(opener):
                                subprocess.Popen([opener, path])
                            else:
                                logger.error(f"Opener '{opener}' not found in PATH.")
                    except Exception as e:
                        logger.error(f"Failed to open file: {e}")
                    continue

                elif cmd["command"] == "save_to_downloads":
                    # Copy the cached file to the User's Download folder
                    src = Path(cmd["path"])
                    if src.exists():
                        # Determine Downloads folder
                        dst_dir = Path.home() / "Downloads"
                        if not dst_dir.exists():
                             logger.error("Could not find Downloads folder")
                        else:
                             dst = dst_dir / src.name
                             try:
                                 shutil.copy2(src, dst)
                                 logger.debug(f"Copied {src.name} to {dst}")
                             except Exception as e:
                                 logger.error(f"Failed to copy file: {e}")
                    else:
                        logger.error(f"Source file not found: {src}")
                    continue

                if cmd.get("service_id") not in backends:
                    logger.debug("Don't know service, ignoring command.")
                    continue

                service = backends[cmd["service_id"]]

                if cmd["command"] == "post_message":
                    logger.debug(f"Sending message to {cmd['service_id']}")
                    await service.post_message(cmd["channel_id"], cmd["body"])

                elif cmd["command"] == "post_reply":
                    logger.debug(f"Sending reply to {cmd['service_id']}")
                    await service.post_reply(cmd["channel_id"], cmd["thread_id"], cmd["body"])

                elif cmd["command"] == "switch_channel":
                    logger.debug(f"Switching channel on {cmd['service_id']}")
                    if hasattr(service, "switch_channel"):
                        await service.switch_channel(cmd["channel_id"], after=cmd.get("after"))

                elif cmd["command"] == "fetch_thread":
                    logger.debug(f"Fetch thread on {cmd['service_id']}")
                    if hasattr(service, "fetch_thread"):
                        await service.fetch_thread(cmd["channel_id"], cmd["thread_id"], after=cmd.get("after"))

                elif cmd["command"] == "message_update":
                    logger.debug(f"Updating message in {cmd['service_id']}")
                    if hasattr(service, "update_message"):
                        await service.update_message(cmd["channel_id"], cmd["message_id"], cmd["body"])

                elif cmd["command"] == "message_delete":
                    logger.debug(f"Deleting message in {cmd['service_id']}")
                    if hasattr(service, "delete_message"):
                        await service.delete_message(cmd["channel_id"], cmd["message_id"])

                elif cmd["command"] == "react":
                    action = cmd["action"]
                    emoji = cmd.get("reaction")
                    logger.debug(f"{action.capitalize()}ing reaction '{emoji}' in {cmd['service_id']}")

                    if action == "add":
                        if hasattr(service, "send_reaction"):
                            await service.send_reaction(cmd["channel_id"], cmd["message_id"], emoji)
                    elif action == "remove":
                        if hasattr(service, "remove_reaction"):
                            await service.remove_reaction(cmd["channel_id"], cmd["message_id"], emoji)

                elif cmd["command"] == "mark_read":
                    logger.debug(f"Marking read in {cmd['service_id']}")
                    if hasattr(service, "mark_channel_read"):
                        await service.mark_channel_read(cmd["channel_id"], cmd["message_id"])

                elif cmd["command"] == "typing":
                    if hasattr(service, "set_typing_status"):
                        await service.set_typing_status(cmd["channel_id"])
                else:
                    logger.warning(f"Received unknown command: {cmd.get('command')}")
            except Exception as e:
                logger.error(f"Error processing command {cmd.get('command')}: {e}")
    except Exception:
        logger.exception("Bad command or main loop error")
    finally:
        sender_task.cancel()
        for backend in backends.values():
            await backend.close()
        await ui_api.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(main())
