"""Test the UI API."""
# todo: check what happens when json we get from ui is invalid

import json
import os
from asyncio import create_task, open_unix_connection
from collections.abc import AsyncGenerator, Iterable
from os import fspath

import pytest

from kbunified.backends import Event
from kbunified.ui import UIAPI, get_receive_socket, get_send_socket


def test_get_send_socket():
    """Test getting a socket."""
    assert fspath(get_send_socket()).endswith("kb_events.sock")


async def async_iterable(iterable: Iterable[Event]):
    """Make iterable async."""
    for item in iterable:
        yield item


@pytest.mark.asyncio
async def test_ui_api_send():
    """Test sending events to the ui."""
    ui_api = UIAPI()

    event1: Event = dict(event="dummy")
    event2: Event = dict(event="dummy2")
    events = async_iterable([event1, event2])
    await ui_api.send_events_to_ui(events)

    (reader, writer) = await open_unix_connection(get_send_socket())

    first_line = await reader.readline()
    assert json.loads(first_line.decode().strip()) == event1

    second_line = await reader.readline()
    assert json.loads(second_line.decode().strip()) == event2

    writer.close()
    await writer.wait_closed()

    await ui_api.close()
    assert not os.path.exists(get_receive_socket())
    assert not os.path.exists(get_send_socket())


@pytest.mark.asyncio
async def test_ui_api_receive():
    """Test receiving commands from the ui."""
    ui_api = UIAPI()
    await ui_api.start_command_reception_server()

    # Open a client connection to the receiving socket.
    _, writer = await open_unix_connection(get_receive_socket())

    # Prepare commands.
    commands = [{"command": "do_something"}, {"command": "do_something_else"}]
    for cmd in commands:
        data = json.dumps(cmd).encode() + b"\n"
        writer.write(data)
        await writer.drain()
    # Closing signals EOF.
    writer.close()
    await writer.wait_closed()

    # Retrieve commands via the async generator.
    received = []
    async for command in ui_api.receive_commands_from_ui():
        received.append(command)
        if len(received) >= len(commands):
            break

    assert received == commands
    await ui_api.close()
    assert not os.path.exists(get_receive_socket())
    assert not os.path.exists(get_send_socket())


@pytest.mark.asyncio
async def test_dummy_ui_echo_round_trip():
    """Test round-trip to ui."""
    ui_api = UIAPI()

    # Start the backend servers.
    await ui_api.send_events_to_ui(one_event())
    await ui_api.start_command_reception_server()

    # Run the dummy UI echo concurrently.
    echo_task = create_task(dummy_ui_echo())

    # Wait for the echoed command from the receiving side.
    # (Since UIAPI._handle_receiving enqueues commands in ui_api.incoming_commands)
    received_command = await anext(ui_api.receive_commands_from_ui())
    expected = {"command": "post", "message": "hello"}
    assert received_command == expected, f"Expected {expected}, got {received_command}"

    # Ensure the dummy UI finishes.
    await echo_task

    # Clean up the backend servers.
    await ui_api.close()


async def one_event() -> AsyncGenerator[Event]:
    """Yield one message event."""
    yield {"event": "message", "message": "hello"}


async def dummy_ui_echo():
    """Dummy UI client that echos incoming messages as post commands."""
    # Connect to the sending socket (backend → UI)
    send_reader, send_writer = await open_unix_connection(get_send_socket())
    # Connect to the receiving socket (UI → backend)
    _, recv_writer = await open_unix_connection(get_receive_socket())

    # Wait for an event from the sending socket.
    line = await send_reader.readline()
    if not line:
        # No event received; close and return.
        send_writer.close()
        await send_writer.wait_closed()
        recv_writer.close()
        await recv_writer.wait_closed()
        return

    # Decode the event.
    event: Event = json.loads(line.decode().strip())
    # Extract the message text; assume the event is in one of these forms.
    message_text = event["message"]

    # Prepare the echo command.
    command = {"command": "post", "message": message_text}
    data = json.dumps(command).encode() + b"\n"

    # Send the command back via the receiving socket.
    recv_writer.write(data)
    await recv_writer.drain()

    # Close both connections.
    send_writer.close()
    await send_writer.wait_closed()
    recv_writer.close()
    await recv_writer.wait_closed()
