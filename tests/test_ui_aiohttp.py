import asyncio

import aiohttp
import pytest

from kbunified.ui import UIAPI

# Mock Config
HOST = "127.0.0.1"
PORT = 4722 # Use distinct test port

@pytest.mark.asyncio
async def test_ui_api_serves_static_and_websocket(tmp_path):
    """Verify that UIAPI:
    1. Serves static files (mocking the Svelte frontend).
    2. Handles WebSocket connections (the API).
    """
    # 1. Setup Mock Frontend
    # Create a dummy index.html in a temp directory
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html>Solaria</html>")

    # 2. Initialize UIAPI with aiohttp
    # We anticipate the refactor: UIAPI will now accept a static_dir path
    ui = UIAPI(static_dir=str(static_dir), port=PORT)

    # Start the server (non-blocking)
    server_task = asyncio.create_task(ui.start())

    # Give it a moment to bind
    await asyncio.sleep(0.5)

    try:
        async with aiohttp.ClientSession() as session:
            # TEST A: Static File Serving (The "Appliance" part)
            async with session.get(f"http://{HOST}:{PORT}/") as resp:
                assert resp.status == 200
                text = await resp.text()
                assert "Solaria" in text  # We got the frontend!

            # TEST B: WebSocket API (The "Brain" part)
            async with session.ws_connect(f"ws://{HOST}:{PORT}/ws") as ws:
                # 1. Send Command
                cmd = {"command": "ping", "service_id": "test"}
                await ws.send_json(cmd)

                # 2. Verify Backend received it
                # (We consume the generator manually for the test)
                received = await ui.receive_commands_from_ui().__anext__()
                assert received["command"] == "ping"

                # 3. Simulate Backend Event
                await ui.send_events_to_ui_list([{"event": "pong"}])

                # 4. Verify Frontend received it
                msg = await ws.receive_json()
                assert msg["event"] == "pong"

    finally:
        await ui.close()
        server_task.cancel()
        try:
            await server_task
        except asyncio.CancelledError:
            pass
