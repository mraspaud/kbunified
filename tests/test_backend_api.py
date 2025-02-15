"""Tests for the main backend api."""

import pytest

from kbunified.backends import ChatBackend, chat_backends_from_config


@pytest.mark.asyncio
async def test_getting_a_message():
    """First test."""
    config: dict[str, dict[str, object]] = dict(my_dummy_service=dict(backend="dummy", name="My dummy service"))
    backend = list(chat_backends_from_config(**config).values())[0]
    await backend.login()
    assert await anext(backend.events())

@pytest.mark.asyncio
async def test_login():
    """Test logging in."""
    config: dict[str, dict[str, object]] = dict(my_dummy_service=dict(backend="dummy", name="My dummy service"))
    backend = list(chat_backends_from_config(**config).values())[0]
    assert backend.is_logged_in() is False
    await backend.login()
    assert backend.is_logged_in() is True

@pytest.mark.asyncio
async def test_get_channel_list():
    """Test getting a channel list."""
    config: dict[str, dict[str, object]] = dict(my_dummy_service=dict(backend="dummy", name="My dummy service"))
    backend = list(chat_backends_from_config(**config).values())[0]
    await backend.login()
    channel_list = backend.get_subbed_channels()
    assert "dummy_channel_1" in channel_list
    assert channel_list["dummy_channel_1"]["name"] == "Dummy Channel #1"

@pytest.mark.asyncio
async def test_post_message():
    """Test posting a message."""
    config: dict[str, dict[str, object]] = dict(my_dummy_service=dict(backend="dummy", name="My dummy service"))
    backend = list(chat_backends_from_config(**config).values())[0]
    await backend.login()
    message_body = "hej"
    await backend.post_message("dummy_channel_1", message_body)
    messages = await get_messages(backend, 2)

    assert message_body in messages

async def get_messages(backend: ChatBackend, count: int):
    """Get some messages from the backend."""
    messages = []
    async for event in backend.events():
        print(event)
        if event["event"] != "message":
            continue
        messages.append(event["message"]["body"])
        if len(messages) >= count:
            return messages

@pytest.mark.asyncio
async def test_backend_closing_after_start():
    """Test stoping the backend after start."""
    config: dict[str, dict[str, object]] = dict(my_dummy_service=dict(backend="dummy", name="My dummy service"))
    backend = list(chat_backends_from_config(**config).values())[0]
    await backend.login()
    events = backend.events()
    assert await anext(events)
    backend.close()
    with pytest.raises(StopAsyncIteration):
        assert await anext(events)
