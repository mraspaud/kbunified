"""Tests for the slack backend."""

import json
from dataclasses import asdict

import keyring
import pytest
from aioresponses import aioresponses

from kbunified.backends.slack import SlackBackend

fake_user_fetch = {"ok": True,
                   "user": {"id": "UGOODOLDME",
                            "team_id": "THEATEAM",
                            "name": "me",
                            "deleted": False,
                            "color": "9f69e7",
                            "real_name": "Mr Me",
                            "tz": "Europe/Amsterdam",
                            "tz_label": "Central European Time",
                            "tz_offset": 3600,
                            "profile": {"title": "Software engineer, founding member of Pytroll",
                                        "phone": "",
                                        "skype": "zorgzorg2",
                                        "real_name": "Mr Me",
                                        "real_name_normalized": "Mr Me",
                                        "display_name": "goodoldme",
                                        "display_name_normalized": "goodoldme",
                                        "fields": {"XfD80ZRD43": {"value": "https://github.com/mraspaud",
                                                                  "alt": ""},
                                                   "XfL79NJB54": {"value": "SMHI, Sweden",
                                                                  "alt": ""},
                                                   "Xf0DAWPUGG": {"value": "zorgzorg2",
                                                                  "alt": ""}},
                                        "status_text": "On holiday",
                                        "status_emoji": ":palm_tree:",
                                        "status_emoji_display_info": [{"emoji_name": "palm_tree",
                                                                       "display_url": "https://a.slack-edge.com/production-standard-emoji-assets/14.0/apple-large/1f334.png",
                                                                       "unicode": "1f334"}],
                                        "status_expiration": 1740178799,
                                        "avatar_hash": "cd41",
                                        "image_original": "https://avatars.slack-edge.com/2023-05-29/534_original.jpg",
                                        "is_custom_image": True,
                                        "email": "good@old.me",
                                        "huddle_state": "default_unset",
                                        "huddle_state_expiration_ts": 0,
                                        "first_name": "Mr",
                                        "last_name": "Me",
                                        "image_24": "https://avatars.slack-edge.com/2023-05-29/534_24.jpg",
                                        "image_32": "https://avatars.slack-edge.com/2023-05-29/534_32.jpg",
                                        "image_48": "https://avatars.slack-edge.com/2023-05-29/534_48.jpg",
                                        "image_72": "https://avatars.slack-edge.com/2023-05-29/534_72.jpg",
                                        "image_192": "https://avatars.slack-edge.com/2023-05-29/534_192.jpg",
                                        "image_512": "https://avatars.slack-edge.com/2023-05-29/534_512.jpg",
                                        "image_1024": "https://avatars.slack-edge.com/2023-05-29/534_1024.jpg",
                                        "status_text_canonical": "Vacationing",
                                        "team": "THEATEAM"},
                            "is_admin": False,
                            "is_owner": False,
                            "is_primary_owner": False,
                            "is_restricted": False,
                            "is_ultra_restricted": False,
                            "is_bot": False,
                   "is_app_user": False,
                   "updated": 1739788021,
                   "is_email_confirmed": True,
                   "has_2fa": True,
                   "two_factor_type": "app",
                   "who_can_share_contact_card": "EVERYONE"}}


@pytest.fixture
def mock_aio():
    """Mock aio responses."""
    with aioresponses() as m:
        yield m


@pytest.mark.asyncio
async def test_message_handling(mock_aio):
    """Test regular message handling."""
    sb = SlackBackend("some_service", "Some Service")
    mock_aio.get("https://api.slack.com/api/users.info?user=UGOODOLDME", status=200, body=json.dumps(fake_user_fetch))
    message = {"type": "message",
               "channel": "DIRECT",
               "text": "<@UGOODOLDME> snt :grin:",
               "blocks": [{"type": "rich_text",
                           "block_id": "tvjUX",
                           "elements": [{"type": "rich_text_section",
                                         "elements": [{"type": "text",
                                                       "text": "snt :grin:"}]}]}],
               "user": "UGOODOLDME",
               "client_msg_id": "de6d0d0c-a83f-4dcd-9d6d-2210e2224f3b",
               "team": "THEATEAM",
               "source_team": "THEATEAM",
               "user_team": "THEATEAM",
               "suppress_notification": False,
               "event_ts": "1739811627.945739",
               "ts": "1739811627.945739"}
    event = await sb.handle_event(message)
    assert event is not None
    assert event["event"] == "message"
    assert event["channel_id"] == "DIRECT"
    assert event["service"]
    assert event["service"]["id"] == "some_service"
    assert event["service"]["name"] == "Some Service"
    msg = event["message"]
    assert msg["id"] == "1739811627.945739"
    assert msg["body"] == "@goodoldme snt 😁"
    assert msg["author"]["display_name"] == "goodoldme"
    assert msg["ts_date"] == "2025-02-17"
    assert msg["ts_time"] == "18:00:27.945739"

    await sb.close()


@pytest.mark.asyncio
async def test_message_reply_handling(mock_aio):
    """Test thread replies."""
    sb = SlackBackend("some_service", "Some Service")
    message = {"type": "message",
               "channel": "CHANNELTOTEST",
               "text": "yes it works",
               "blocks": [{"type": "rich_text", "block_id": "x51DJ",
                           "elements": [{"type": "rich_text_section",
                                         "elements": [{"type": "text", "text": "yes it works"}]}]}],
               "user": "UGOODOLDME",
               "client_msg_id": "c5f6a847-b3aa-4c46-9531-59aae6e6ec80",
               "team": "THEATEAM",
               "source_team": "THEATEAM",
               "user_team": "THEATEAM",
               "suppress_notification": False,
               "event_ts": "1739879348.328229",
               "ts": "1739879348.328229"}

    reply1 = {"type": "message",
              "subtype": "message_replied",
              "message": {"user": "UGOODOLDME",
                          "type": "message",
                          "ts": "1739879348.328229",
                          "client_msg_id": "c5f6a847-b3aa-4c46-9531-59aae6e6ec80",
                          "text": "yes it works",
                          "team": "THEATEAM",
                          "thread_ts": "1739879348.328229",
                          "reply_count": 1,
                          "reply_users_count": 1,
                          "latest_reply": "1739879722.353889",
                          "reply_users": ["UGOODOLDME"],
                          "is_locked": False,
                          "blocks": [{"type": "rich_text",
                                      "block_id": "x51DJ",
                                      "elements": [{"type": "rich_text_section",
                                                    "elements": [{"type": "text",
                                                                  "text": "yes it works"}]}]}]},
              "channel": "CHANNELTOTEST",
              "hidden": True,
              "ts": "1739879348.328229",
              "event_ts": "1739879722.000100"}

    reply2 = {"type": "message",
              "channel": "CHANNELTOTEST",
              "text": "indeed",
              "blocks": [{"type": "rich_text",
                          "block_id": "GiAtP",
                          "elements": [{"type": "rich_text_section",
                                        "elements": [{"type": "text",
                                                      "text": "indeed"}]}]}],
              "user": "UGOODOLDME",
              "client_msg_id": "8f692382-57b7-40f6-ab9a-7ccd14214607",
              "team": "THEATEAM",
              "source_team": "THEATEAM",
              "user_team": "THEATEAM",
              "thread_ts": "1739879348.328229",
              "suppress_notification": False,
              "event_ts": "1739879722.353889",
              "ts": "1739879722.353889"}

    sb = SlackBackend("some_service", "Some Service")
    mock_aio.get("https://api.slack.com/api/users.info?user=UGOODOLDME", status=200, body=json.dumps(fake_user_fetch))
    event = await sb.handle_event(message)
    event2 = await sb.handle_event(reply1)
    assert "replies" in event2["message"]
    event = await sb.handle_event(reply2)
    assert event is not None
    assert event["message"]["thread_id"] == "1739879348.328229"
    await sb.close()


@pytest.mark.asyncio
async def test_message_reactions(mock_aio):
    """Test message reactions."""
    message1 = {"type": "message", "channel": "CHANNELTOTEST", "text": "hi", "blocks": [{"type": "rich_text", "block_id": "a8bcU", "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": "hi"}]}]}], "user": "UGOODOLDME", "client_msg_id": "da53108e-4a3a-4630-a59b-e0e11e8f131e", "team": "THEATEAM", "source_team": "THEATEAM", "user_team": "THEATEAM", "suppress_notification": False, "event_ts": "1739888306.431719", "ts": "1739888306.431719"}
    message2 = {"type": "message", "channel": "CHANNELTOTEST", "text": "hi2", "blocks": [{"type": "rich_text", "block_id": "20eZ7", "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": "hi2"}]}]}], "user": "UGOODOLDME", "client_msg_id": "087ff75b-a998-4970-baa0-5e9438b169de", "team": "THEATEAM", "source_team": "THEATEAM", "user_team": "THEATEAM", "suppress_notification": False, "event_ts": "1739888342.743829", "ts": "1739888342.743829"}
    reaction_to_2 = {"type": "reaction_added", "user": "UGOODOLDME", "reaction": "pray", "item": {"type": "message", "channel": "CHANNELTOTEST", "ts": "1739888342.743829"}, "item_user": "UGOODOLDME", "event_ts": "1739888360.000300", "ts": "1739888360.000300"}
    reaction_to_1 = {"type": "reaction_added", "user": "UGOODOLDME", "reaction": "grin", "item": {"type": "message", "channel": "CHANNELTOTEST", "ts": "1739888306.431719"}, "item_user": "UGOODOLDME", "event_ts": "1739888389.000400", "ts": "1739888389.000400"}
    reaction_to_1_2 = {"type": "reaction_added", "user": "UGOODOLDME", "reaction": "pray", "item": {"type": "message", "channel": "CHANNELTOTEST", "ts": "1739888306.431719"}, "item_user": "UGOODOLDME", "event_ts": "1739888389.000400", "ts": "1739888389.000400"}
    reaction_to_1_remove = {"type": "reaction_removed", "user": "UGOODOLDME", "reaction": "pray", "item": {"type": "message", "channel": "CHANNELTOTEST", "ts": "1739888306.431719"}, "item_user": "UGOODOLDME", "event_ts": "1739898031.001200", "ts": "1739898031.001200"}
    reaction_to_1_remove_again = {"type": "reaction_removed", "user": "UGOODOLDME", "reaction": "grin", "item": {"type": "message", "channel": "CHANNELTOTEST", "ts": "1739888306.431719"}, "item_user": "UGOODOLDME", "event_ts": "1739898031.001200", "ts": "1739898031.001200"}

    sb = SlackBackend("some_service", "Some Service")
    mock_aio.get("https://api.slack.com/api/users.info?user=UGOODOLDME", status=200, body=json.dumps(fake_user_fetch))
    event1 = await sb.handle_event(message1)
    event2 = await sb.handle_event(message2)
    event2u = await sb.handle_event(reaction_to_2)
    event1u = await sb.handle_event(reaction_to_1)
    assert list(event2u["message"]["reactions"].keys()) == ["🙏"]
    event2u["message"].pop("reactions")
    assert event2u == event2
    assert list(event1u["message"]["reactions"].keys()) == ["😁"]
    event1u2 = await sb.handle_event(reaction_to_1_2)
    assert list(event1u2["message"]["reactions"].keys()) == ["😁", "🙏"]
    event1u3 = await sb.handle_event(reaction_to_1_remove)
    assert list(event1u3["message"]["reactions"].keys()) == ["😁"]
    event1u4 = await sb.handle_event(reaction_to_1_remove_again)
    assert event1u4 == event1
    await sb.close()


@pytest.mark.asyncio
async def test_message_attachments(mock_aio):
    """Test file attachments."""
    file_creation = {"type": "file_created",
                     "file_id": "FILETOTEST",
                     "user_id": "UGOODOLDME",
                     "file": {"id": "FILETOTEST"},
                     "event_ts": "1739899058.004300"}
    message_with_file = {"type": "message",
                         "channel": "CHANNELTOTEST",
                         "text": "attach",
                         "blocks": [{"type": "rich_text", "block_id": "Xb9A2", "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": "attach"}]}]}],
                         "user": "UGOODOLDME",
                         "files": [{"id": "FILETOTEST",
                                    "created": 1739899056,
                                    "timestamp": 1739899056,
                                    "name": "Screenshot from 2025-02-05 15-16-41.png",
                                    "title": "Screenshot from 2025-02-05 15-16-41.png",
                                    "mimetype": "image/png",
                                    "filetype": "png",
                                    "pretty_type": "PNG",
                                    "user": "UGOODOLDME",
                                    "user_team": "THEATEAM",
                                    "editable": False,
                                    "size": 4516,
                                    "mode": "hosted",
                                    "is_external": False,
                                    "external_type": "",
                                    "is_public": False,
                                    "public_url_shared": False,
                                    "display_as_bot": False,
                                    "username": "",
                                    "url_private": "https://files.slack.com/files-pri/THEATEAM-FILETOTEST/screenshot_from_2025-02-05_15-16-41.png",
                                    "url_private_download": "https://files.slack.com/files-pri/THEATEAM-FILETOTEST/download/screenshot_from_2025-02-05_15-16-41.png",
                                    "media_display_type": "unknown",
                                    "thumb_64": "https://files.slack.com/files-tmb/THEATEAM-FILETOTEST-9848bebf92/screenshot_from_2025-02-05_15-16-41_64.png",
                                    "thumb_80": "https://files.slack.com/files-tmb/THEATEAM-FILETOTEST-9848bebf92/screenshot_from_2025-02-05_15-16-41_80.png",
                                    "thumb_360": "https://files.slack.com/files-tmb/THEATEAM-FILETOTEST-9848bebf92/screenshot_from_2025-02-05_15-16-41_360.png",
                                    "thumb_360_w": 310,
                                    "thumb_360_h": 27,
                                    "thumb_160": "https://files.slack.com/files-tmb/THEATEAM-FILETOTEST-9848bebf92/screenshot_from_2025-02-05_15-16-41_160.png",
                                    "original_w": 310,
                                    "original_h": 27,
                                    "thumb_tiny": "AwAEADCkG6Agcc9KN3sPypB1pKYDxIQMbVP1FBlLdVX8qYetAoAekhVgQF9OlK7l85AHPYUwdR9aD1P1oA//2Q==",
                                    "permalink": "https://pytroll.slack.com/files/UGOODOLDME/FILETOTEST/screenshot_from_2025-02-05_15-16-41.png",
                                    "permalink_public": "https://slack-files.com/THEATEAM-FILETOTEST-639c16fd94",
                                    "is_starred": False,
                                    "has_rich_preview": False,
                                    "file_access": "visible"}],
                         "client_msg_id": "016fa7f4-9a86-4b34-879e-e03068ea2758",
                         "team": "THEATEAM",
                         "source_team": "THEATEAM",
                         "user_team": "THEATEAM",
                         "display_as_bot": False,
                         "upload": False,
                         "event_ts": "1739899061.907259",
                         "ts": "1739899061.907259"}
    sb = SlackBackend("some_service", "Some Service")
    mock_aio.get("https://api.slack.com/api/users.info?user=UGOODOLDME", status=200, body=json.dumps(fake_user_fetch))
    mock_aio.get("https://files.slack.com/files-pri/THEATEAM-FILETOTEST/screenshot_from_2025-02-05_15-16-41.png", status=200)
    event1 = await sb.handle_event(message_with_file)
    assert event1["message"]["body"] == ("attach\n![Screenshot from 2025-02-05 15-16-41.png]"
                                         "(/tmp/FILETOTEST_Screenshot_from_2025-02-05_15-16-41.png)")
    await sb.close()

@pytest.mark.asyncio
async def test_post_message(mock_aio, monkeypatch):
    def dummy_kr(group, item):
        return f"{group}:{item}"
    monkeypatch.setattr(keyring, "get_password", dummy_kr)
    mock_aio.get("https://api.slack.com/api/users.info?user=UGOODOLDME", status=200, body=json.dumps(fake_user_fetch))
    message = {"type": "message",
               "channel": "DIRECT",
               "text": "<@UGOODOLDME> snt :grin:",
               "blocks": [{"type": "rich_text",
                           "block_id": "tvjUX",
                           "elements": [{"type": "rich_text_section",
                                         "elements": [{"type": "text",
                                                       "text": "snt :grin:"}]}]}],
               "user": "UGOODOLDME",
               "client_msg_id": "de6d0d0c-a83f-4dcd-9d6d-2210e2224f3b",
               "team": "THEATEAM",
               "source_team": "THEATEAM",
               "user_team": "THEATEAM",
               "suppress_notification": False,
               "event_ts": "1739811627.945739",
               "ts": "1739811627.945739"}
    sb = SlackBackend("some_service", "Some Service")
    await sb.handle_event(message)
    mock_aio.post("https://api.slack.com/api/chat.postMessage", status=200, payload=dict(ok="yep"))
    channel = "CHANNELTOTEST"
    text = "hello @goodoldme"
    await sb.post_message(channel, text)
    modified_text = "hello <@UGOODOLDME>"
    mock_aio.assert_called_with("https://api.slack.com/api/chat.postMessage", method="POST",
                                headers=sb.get_headers(),
                                ssl=sb.ssl_ctx,
                                json=dict(channel=channel, text=modified_text))
    await sb.close()

@pytest.mark.asyncio
async def test_delete_message(mock_aio):
    deleted_message = {"type": "message",
                       "subtype": "message_deleted",
                       "previous_message": {"user": "UGOODOLDME",
                                            "type": "message",
                                            "ts": "1739902122.384779",
                                            "text": "Third time’s the charm",
                                            "team": "THEATEAM",
                                            "blocks": [{"type": "rich_text", "block_id": "dHcI", "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": "Third time’s the charm"}]}]}]},
                       "channel": "CHANNELTOTEST",
                       "hidden": True,
                       "deleted_ts": "1739902122.384779",
                       "event_ts": "1739917830.006200",
                       "ts": "1739917830.006200"}
    sb = SlackBackend("some_service", "Some Service")
    mock_aio.get("https://api.slack.com/api/users.info?user=UGOODOLDME", status=200, body=json.dumps(fake_user_fetch))
    event1 = await sb.handle_event(deleted_message)
    assert event1["event"] == "deleted_message"
    assert event1["channel_id"] == "CHANNELTOTEST"
    assert event1["message_id"] == "1739902122.384779"
    await sb.close()


fake_counts = dict(
        ok=True,
        threads=dict(
            has_unreads=False,
            mention_count=0),
        channels=[dict(id="CHANNELTOTEST",
                       mention_count=0,
                       has_unreads=False),
                  dict(id="CANOTHER",
                       mention_count=2,
                       has_unreads=True)])

fake_boot = dict(
        ok=True,
        channels=[dict(
            id="CHANNELTOTEST",
            name="test",
            is_member=True,
            is_archived=False,
            topic=dict(value="For testing")),
                  dict(
            id="CANOTHER",
            name="another",
            is_member=True,
            is_archived=False,
            topic=dict(value="Another channel")),
                  dict(
            id="CARCHIVED",
            name="archived",
            is_member=True,
            is_archived=True,
            topic=dict(value="Archived channel"))],
        starred=["CHANNELTOTEST"])



@pytest.mark.asyncio
async def test_fetch_channels(mock_aio):
    """Test fetching channels."""
    sb = SlackBackend("some_service", "Some Service")
    mock_aio.post("https://api.slack.com/api/client.userBoot", status=200, payload=fake_boot)
    mock_aio.post("https://api.slack.com/api/client.counts", status=200, payload=fake_counts)
    channels = await sb.get_subbed_channels()
    assert len(channels) == 2
    tchan = asdict(channels[0])
    assert tchan["name"] == "test"
    assert tchan["unread"] is False
    assert tchan["topic"] == "For testing"
    assert tchan["starred"] is True
    achan = asdict(channels[1])
    assert achan["name"] == "another"
    assert achan["unread"] is True
    assert achan["mentions"] == 2
    assert achan["starred"] is False
    await sb.close()


fake_history = {"ok": True,
                "messages": [{"user": "UGOODOLDME",
                              "type": "message", "ts": "1739961050.883929",
                              "client_msg_id": "7c656808-0559-4279-87a2-953323461541",
                              "text": "hi",
                              "team": "THEATEAM",
                              "blocks": [{"type": "rich_text", "block_id": "a8bcU", "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": "hi"}]}]}]},
                             {"user": "UGOODOLDME",
                              "type": "message",
                              "ts": "1739960952.233069",
                              "client_msg_id": "f6a868a3-58a6-470a-a4a1-89b75d322033",
                              "text": "god morgon",
                              "team": "THEATEAM",
                              "blocks": [{"type": "rich_text", "block_id": "JKIP1", "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": "god morgon"}]}]}]},
                             {"user": "UGOODOLDME",
                              "type": "message",
                              "ts": "1739959721.414509",
                              "client_msg_id": "d000f62b-6acb-4570-91ff-a105bce78cb5",
                              "text": "good morning",
                              "team": "THEATEAM",
                              "reply_count": 2,
                              "reply_users_count": 1,
                              "reply_users": ["UGOODOLDME"],
                              "subscribed": False,
                              "blocks": [{"type": "rich_text", "block_id": "ep6zy", "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": "good morning"}]}]}]},
                             {"user": "UGOODOLDME",
                              "type": "message",
                              "ts": "1739918877.019159",
                              "client_msg_id": "f7223e4d-a770-422a-92d5-e1255cb8c6a7",
                              "text": "hi",
                              "team": "THEATEAM",
                              "reactions": [{"name": "grin", "users": ["USER1", "USER2"]},
                                            {"name": "pray", "users": ["USER3", "USER4"]}],
                              "blocks": [{"type": "rich_text", "block_id": "a8bcU", "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": "hi"}]}]}]}],
                "has_more": True,
                "pin_count": 0,
                "channel_actions_ts": None,
                "channel_actions_count": 0,
                "response_metadata": {"next_cursor": "bmV4dF90czoxNzM5OTE3Mjg0NzYxMzc5"}}


@pytest.mark.asyncio
async def test_fetch_history(mock_aio):
    """Test fetching channel history."""
    sb = SlackBackend("some service", "Some Service")
    mock_aio.get("https://api.slack.com/api/users.info?user=UGOODOLDME", status=200, body=json.dumps(fake_user_fetch))
    mock_aio.get("https://api.slack.com/api/conversations.history?channel=some_channel&limit=4", status=200, payload=fake_history)
    await sb.switch_channel("some_channel", limit=4)
    messages = [sb._inbox.get_nowait() for i in range(4)]
    msg = json.loads(messages[0])
    assert msg["type"] == "message"
    event = await sb.handle_event(msg)
    assert "reactions" in event["message"]
    assert len(event["message"]["reactions"]) == 2
    assert list(event["message"]["reactions"].keys()) == ["😁", "🙏"]

    event = await sb.handle_event(json.loads(messages[1]))
    replies = event["message"]["replies"]
    assert replies["count"] == 2
    assert replies["users"] == ["UGOODOLDME"]

    await sb.switch_channel("some_channel", limit=4)  # fails if a connection attempt is made
    await sb.close()


fake_replies = {"ok": True,
                "messages": [{"user": "UGOODOLDME",
                              "type": "message",
                              "ts": "1740322860.950559",
                              "client_msg_id": "0dfa9963-a809-4800-8a95-4e9e9f61326a",
                              "text": "hej",
                              "team": "THEATEAM",
                              "thread_ts": "1740322860.950559",
                              "reply_count": 2,
                              "reply_users_count": 1,
                              "latest_reply": "1740322900.605329",
                              "reply_users": ["UGOODOLDME"],
                              "is_locked": False,
                              "subscribed": True,
                              "last_read": "1740322900.605329",
                              "blocks": [{"type": "rich_text",
                                          "block_id": "83YUt",
                                          "elements": [{"type": "rich_text_section",
                                                        "elements": [{"type": "text",
                                                                      "text": "hej"}]}]}]},
                             {"user": "UGOODOLDME",
                              "type": "message",
                              "ts": "1740322890.536109",
                              "client_msg_id": "062ec7ab-d733-4edb-add4-67cbfbcbb728",
                              "text": "ja",
                              "team": "THEATEAM",
                              "thread_ts": "1740322860.950559",
                              "parent_user_id": "UGOODOLDME",
                              "blocks": [{"type": "rich_text",
                                          "block_id": "nbAaG",
                                          "elements": [{"type": "rich_text_section",
                                                        "elements": [{"type": "text",
                                                                      "text": "ja"}]}]}]},
                             {"user": "UGOODOLDME",
                              "type": "message",
                              "ts": "1740322900.605329",
                              "client_msg_id": "ce77eaf4-7983-4acb-9484-250447fc3f52",
                              "text": "funkar?",
                              "team": "THEATEAM",
                              "thread_ts": "1740322860.950559",
                              "parent_user_id": "UGOODOLDME",
                              "blocks": [{"type": "rich_text",
                                          "block_id": "+eSgM",
                                          "elements": [{"type": "rich_text_section",
                                                        "elements": [{"type": "text",
                                                                      "text": "funkar?"}]}]}]}],
                "has_more": False}

@pytest.mark.asyncio
# async def test_fetch_thread():
async def test_fetch_thread(mock_aio):
    """Test fetching channel history."""
    sb = SlackBackend("some service", "Some Service")
    mock_aio.get("https://api.slack.com/api/users.info?user=UGOODOLDME", status=200, body=json.dumps(fake_user_fetch))
    mock_aio.get("https://api.slack.com/api/conversations.replies?channel=some_channel&ts=1740322860.950559", status=200, payload=fake_replies)
    await sb.fetch_thread("some_channel", "1740322860.950559")
    messages = [sb._inbox.get_nowait() for i in range(3)]
    msg = json.loads(messages[1])
    assert msg["type"] == "message"
    event = await sb.handle_event(msg)
    assert event["message"]["body"] == "ja"
    await sb.close()


first_message = {"type": "message",
                 "channel": "CHANNELTOTEST",
                 "text": "hi",
                 "blocks": [{"type": "rich_text", "block_id": "a8bcU", "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": "hi"}]}]}],
                 "user": "UGOODOLDME",
                 "client_msg_id": "5c5f423a-243a-409c-aa12-ba87924bff68",
                 "team": "THEATEAM",
                 "source_team": "THEATEAM",
                 "user_team": "THEATEAM",
                 "suppress_notification": False,
                 "event_ts": "1740060997.732449",
                 "ts": "1740060997.732449"}
edit_message = {"type": "message",
                "subtype": "message_changed",
                "message": {"user": "UGOODOLDME",
                            "type": "message",
                            "edited": {"user": "UGOODOLDME",
                                       "ts": "1740061002.000000"},
                            "client_msg_id": "5c5f423a-243a-409c-aa12-ba87924bff68",
                            "text": "hello",
                            "team": "THEATEAM",
                            "blocks": [{"type": "rich_text", "block_id": "ZL1yL", "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": "hello"}]}]}],
                            "ts": "1740060997.732449",
                            "source_team": "THEATEAM",
                            "user_team": "THEATEAM"},
                "previous_message": {"user": "UGOODOLDME",
                                     "type": "message",
                                     "ts": "1740060997.732449",
                                     "client_msg_id": "5c5f423a-243a-409c-aa12-ba87924bff68",
                                     "text": "hi",
                                     "team": "THEATEAM",
                                     "blocks": [{"type": "rich_text", "block_id": "a8bcU", "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": "hi"}]}]}]},
                "channel": "CHANNELTOTEST",
                "hidden": True,
                "ts": "1740060997.732449",
                "event_ts": "1740061002.000200"}


@pytest.mark.asyncio
async def test_message_edited(mock_aio):
    """Test regular message handling."""
    sb = SlackBackend("some_service", "Some Service")
    mock_aio.get("https://api.slack.com/api/users.info?user=UGOODOLDME", status=200, body=json.dumps(fake_user_fetch))
    event = await sb.handle_event(first_message)
    assert event is not None
    assert event["event"] == "message"
    msg = event["message"]
    assert msg["body"] == "hi"
    assert msg["ts_date"] == "2025-02-20"
    assert msg["ts_time"] == "15:16:37.732449"


    event = await sb.handle_event(edit_message)
    assert event is not None
    assert event["event"] == "message"
    msg = event["message"]
    assert msg["body"] == "hello"
    assert msg["ts_date"] == "2025-02-20"
    assert msg["ts_time"] == "15:16:37.732449"
    assert msg["edit_time"] == "15:16:42"
    await sb.close()


#####
# reply with copy to channel, then change reply

"""
stderr: DEBUG:slack_backend:Received data: {'type': 'message', 'channel': 'CHANNELTOTEST', 'text': 'hi', 'blocks': [{'type': 'rich_text', 'block_id': 'a8bcU', 'elements': [{'type': 'rich_text_section', 'elements': [{'type': 'text', 'text': 'hi'}]}]}], 'user': 'UGOODOLDME', 'client_msg_id': '0267354b-049d-4609-85cb-391e9e89c1f3', 'team': 'THEATEAM', 'source_team': 'THEATEAM', 'user_team': 'THEATEAM', 'suppress_notification': False, 'event_ts': '1740130139.359819', 'ts': '1740130139.359819'}
stderr: DEBUG:slack_backend:Received data: {'type': 'message', 'subtype': 'message_replied', 'message': {'user': 'UGOODOLDME', 'type': 'message', 'ts': '1740130139.359819', 'client_msg_id': '0267354b-049d-4609-85cb-391e9e89c1f3', 'text': 'hi', 'team': 'THEATEAM', 'thread_ts': '1740130139.359819', 'reply_count': 1, 'reply_users_count': 1, 'latest_reply': '1740130168.429989', 'reply_users': ['UGOODOLDME'], 'is_locked': False, 'blocks': [{'type': 'rich_text', 'block_id': 'a8bcU', 'elements': [{'type': 'rich_text_section', 'elements': [{'type': 'text', 'text': 'hi'}]}]}]}, 'channel': 'CHANNELTOTEST', 'hidden': True, 'ts': '1740130139.359819', 'event_ts': '1740130168.000100'}
stderr: DEBUG:slack_backend:Received data: {'type': 'message', 'subtype': 'thread_broadcast', 'channel': 'CHANNELTOTEST', 'text': 'hello too', 'blocks': [{'type': 'rich_text', 'block_id': '8xuO6', 'elements': [{'type': 'rich_text_section', 'elements': [{'type': 'text', 'text': 'hello too'}]}]}], 'user': 'UGOODOLDME', 'client_msg_id': 'd8a38d56-cd38-4b5a-b27b-1085d1b7d8d9', 'source_team': 'THEATEAM', 'user_team': 'THEATEAM', 'thread_ts': '1740130139.359819', 'root': {'user': 'UGOODOLDME', 'type': 'message', 'ts': '1740130139.359819', 'client_msg_id': '0267354b-049d-4609-85cb-391e9e89c1f3', 'text': 'hi', 'team': 'THEATEAM', 'blocks': [{'type': 'rich_text', 'block_id': 'a8bcU', 'elements': [{'type': 'rich_text_section', 'elements': [{'type': 'text', 'text': 'hi'}]}]}]}, 'suppress_notification': False, 'event_ts': '1740130168.429989', 'ts': '1740130168.429989'}

stderr: DEBUG:slack_backend:Received data: {'type': 'message', 'subtype': 'message_changed', 'message': {'subtype': 'thread_broadcast', 'user': 'UGOODOLDME', 'thread_ts': '1740130139.359819', 'root': {'user': 'UGOODOLDME', 'type': 'message', 'ts': '1740130139.359819', 'client_msg_id': '0267354b-049d-4609-85cb-391e9e89c1f3', 'text': 'hi', 'team': 'THEATEAM', 'thread_ts': '1740130139.359819', 'reply_count': 1, 'reply_users_count': 1, 'latest_reply': '1740130168.429989', 'reply_users': ['UGOODOLDME'], 'is_locked': False, 'blocks': [{'type': 'rich_text', 'block_id': 'a8bcU', 'elements': [{'type': 'rich_text_section', 'elements': [{'type': 'text', 'text': 'hi'}]}]}]}, 'type': 'message', 'client_msg_id': 'd8a38d56-cd38-4b5a-b27b-1085d1b7d8d9', 'text': 'hello too', 'blocks': [{'type': 'rich_text', 'block_id': '8xuO6', 'elements': [{'type': 'rich_text_section', 'elements': [{'type': 'text', 'text': 'hello too'}]}]}], 'ts': '1740130168.429989'}, 'previous_message': {'subtype': 'thread_broadcast', 'user': 'UGOODOLDME', 'thread_ts': '1740130139.359819', 'root': {'user': 'UGOODOLDME', 'type': 'message', 'ts': '1740130139.359819', 'client_msg_id': '0267354b-049d-4609-85cb-391e9e89c1f3', 'text': 'hi', 'team': 'THEATEAM', 'thread_ts': '1740130139.359819', 'reply_count': 1, 'reply_users_count': 1, 'latest_reply': '1740130168.429989', 'reply_users': ['UGOODOLDME'], 'is_locked': False, 'blocks': [{'type': 'rich_text', 'block_id': 'a8bcU', 'elements': [{'type': 'rich_text_section', 'elements': [{'type': 'text', 'text': 'hi'}]}]}]}, 'type': 'message', 'ts': '1740130168.429989', 'client_msg_id': 'd8a38d56-cd38-4b5a-b27b-1085d1b7d8d9', 'text': 'hello too', 'blocks': [{'type': 'rich_text', 'block_id': '8xuO6', 'elements': [{'type': 'rich_text_section', 'elements': [{'type': 'text', 'text': 'hello too'}]}]}]}, 'channel': 'CHANNELTOTEST', 'hidden': True, 'ts': '1740130168.429989', 'event_ts': '1740130168.000200'}

stderr: DEBUG:slack_backend:Received data: {'type': 'message', 'subtype': 'message_changed', 'message': {'subtype': 'thread_broadcast', 'user': 'UGOODOLDME', 'thread_ts': '1740130139.359819', 'root': {'user': 'UGOODOLDME', 'type': 'message', 'ts': '1740130139.359819', 'client_msg_id': '0267354b-049d-4609-85cb-391e9e89c1f3', 'text': 'hi', 'team': 'THEATEAM', 'thread_ts': '1740130139.359819', 'reply_count': 1, 'reply_users_count': 1, 'latest_reply': '1740130168.429989', 'reply_users': ['UGOODOLDME'], 'is_locked': False, 'blocks': [{'type': 'rich_text', 'block_id': 'a8bcU', 'elements': [{'type': 'rich_text_section', 'elements': [{'type': 'text', 'text': 'hi'}]}]}]}, 'type': 'message', 'edited': {'user': 'UGOODOLDME', 'ts': '1740130417.000000'}, 'client_msg_id': 'd8a38d56-cd38-4b5a-b27b-1085d1b7d8d9', 'text': 'hello too 2', 'blocks': [{'type': 'rich_text', 'block_id': '5bkf3', 'elements': [{'type': 'rich_text_section', 'elements': [{'type': 'text', 'text': 'hello too 2'}]}]}], 'ts': '1740130168.429989', 'source_team': 'THEATEAM', 'user_team': 'THEATEAM'}, 'previous_message': {'subtype': 'thread_broadcast', 'user': 'UGOODOLDME', 'thread_ts': '1740130139.359819', 'root': {'user': 'UGOODOLDME', 'type': 'message', 'ts': '1740130139.359819', 'client_msg_id': '0267354b-049d-4609-85cb-391e9e89c1f3', 'text': 'hi', 'team': 'THEATEAM', 'thread_ts': '1740130139.359819', 'reply_count': 1, 'reply_users_count': 1, 'latest_reply': '1740130168.429989', 'reply_users': ['UGOODOLDME'], 'is_locked': False, 'subscribed': True, 'last_read': '1740130168.429989', 'blocks': [{'type': 'rich_text', 'block_id': 'a8bcU', 'elements': [{'type': 'rich_text_section', 'elements': [{'type': 'text', 'text': 'hi'}]}]}]}, 'type': 'message', 'ts': '1740130168.429989', 'client_msg_id': 'd8a38d56-cd38-4b5a-b27b-1085d1b7d8d9', 'text': 'hello too', 'blocks': [{'type': 'rich_text', 'block_id': '8xuO6', 'elements': [{'type': 'rich_text_section', 'elements': [{'type': 'text', 'text': 'hello too'}]}]}]}, 'channel': 'CHANNELTOTEST', 'hidden': True, 'ts': '1740130168.429989', 'event_ts': '1740130417.000300'}
"""
