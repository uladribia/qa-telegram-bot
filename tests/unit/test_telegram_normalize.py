# SPDX-License-Identifier: MIT
"""Tests for the Telegram inbound normalizer."""

from datetime import UTC, datetime

from knowledge_bot.adapters.telegram.identity import TelegramIdentity
from knowledge_bot.adapters.telegram.models import TelegramUpdate
from knowledge_bot.adapters.telegram.normalize import (
    is_valid_webhook_secret,
    normalize_callback,
    normalize_message,
)
from knowledge_bot.domain.enums import ContentType
from knowledge_bot.models.messages import MemberChange

ALLOWED_CHAT = "-1001234567890"
IDENTITY = TelegramIdentity(
    admin_user_id="100000001",
    bot_id="999",
    bot_username="bhc_qa_testbot",
)


def _message(**overrides: object) -> dict:
    message: dict = {
        "message_id": 10,
        "date": 1789000000,
        "chat": {"id": int(ALLOWED_CHAT), "type": "supergroup"},
        "from": {"id": 111, "is_bot": False},
        "text": "hola",
    }
    message.update(overrides)
    return message


def _update(message: dict | None = None, **overrides: object) -> TelegramUpdate:
    payload: dict = {
        "update_id": 1,
        "message": message if message is not None else _message(),
    }
    payload.update(overrides)
    return TelegramUpdate.model_validate(payload)


def test_text_message_normalization() -> None:
    """A plain text message becomes a normalized text message."""
    normalized = normalize_message(_update(), IDENTITY)
    assert normalized is not None
    assert normalized.source.kind == "telegram"
    assert normalized.text == "hola"
    assert normalized.content_type is ContentType.TEXT
    assert normalized.timestamp == datetime.fromtimestamp(1789000000, tz=UTC)
    assert normalized.source_message_id == f"{ALLOWED_CHAT}:10"


def test_reply_to_bot_is_flagged() -> None:
    """A reply to a bot message is flagged as addressing the bot."""
    reply = _message(message_id=9, **{"from": {"id": 999, "is_bot": True}})
    normalized = normalize_message(_update(_message(reply_to_message=reply)), IDENTITY)
    assert normalized is not None
    assert normalized.is_reply_to_bot is True
    assert normalized.reply_to_message_id == "9"


def test_reply_to_human_is_not_addressed() -> None:
    """A reply to another member is not addressing the bot."""
    reply = _message(message_id=9, **{"from": {"id": 222, "is_bot": False}})
    normalized = normalize_message(_update(_message(reply_to_message=reply)), IDENTITY)
    assert normalized is not None
    assert normalized.is_reply_to_bot is False


def test_mention_is_detected() -> None:
    """Text mentioning the bot username is flagged."""
    message = _message(text="@bhc_qa_testbot quan entrenen?")
    normalized = normalize_message(_update(message), IDENTITY)
    assert normalized is not None
    assert normalized.mentions_bot is True


def test_photo_is_metadata_only() -> None:
    """Photos produce an attachment reference and no text."""
    photo = [
        {"file_id": "small", "file_unique_id": "u1", "width": 90, "height": 45},
        {
            "file_id": "large",
            "file_unique_id": "u2",
            "width": 1280,
            "height": 720,
            "file_size": 4096,
        },
    ]
    normalized = normalize_message(_update(_message(text=None, photo=photo)), IDENTITY)
    assert normalized is not None
    assert normalized.content_type is ContentType.IMAGE
    assert normalized.text is None
    assert len(normalized.attachments) == 1
    attachment = normalized.attachments[0]
    assert attachment.kind == "image"
    assert attachment.external_id == "large"
    assert attachment.width == 1280
    assert attachment.processing_status.value == "unprocessed"


def test_caption_is_kept_as_text() -> None:
    """A photo caption is kept as searchable text."""
    photo = [{"file_id": "large", "file_unique_id": "u2", "width": 100, "height": 100}]
    message = _message(text=None, photo=photo, caption="la samarreta nova")
    normalized = normalize_message(_update(message), IDENTITY)
    assert normalized is not None
    assert normalized.content_type is ContentType.IMAGE
    assert normalized.text == "la samarreta nova"


def test_other_group_is_normalized_for_binding_resolution() -> None:
    """The adapter leaves group authorization to durable binding resolution."""
    message = _message(chat={"id": -1, "type": "supergroup"})
    assert normalize_message(_update(message), IDENTITY) is not None


def test_direct_message_is_marked_and_allowed() -> None:
    """Private chats are accepted and flagged as direct messages."""
    message = _message(chat={"id": 555, "type": "private"})
    normalized = normalize_message(_update(message), IDENTITY)
    assert normalized is not None
    assert normalized.is_direct_message is True


def test_admin_sender_is_flagged() -> None:
    """The configured admin user id flags the sender as admin."""
    message = _message(**{"from": {"id": 100000001, "is_bot": False}})
    normalized = normalize_message(_update(message), IDENTITY)
    assert normalized is not None
    assert normalized.sender_is_admin is True


def test_sender_is_pseudonymized() -> None:
    """Sender ids are hashes, never raw Telegram ids."""
    normalized = normalize_message(_update(), IDENTITY)
    assert normalized is not None
    assert normalized.sender_id is not None
    assert normalized.sender_id != "111"
    assert "111" not in normalized.sender_id


def test_non_message_update_is_ignored() -> None:
    """Updates without a message are ignored by the message normalizer."""
    assert (
        normalize_message(TelegramUpdate.model_validate({"update_id": 2}), IDENTITY)
        is None
    )


def test_callback_normalization() -> None:
    """A callback query is reduced to its normalized form."""
    payload = {
        "update_id": 3,
        "callback_query": {
            "id": "cb-1",
            "from": {"id": 111, "is_bot": False},
            "data": "feedback:start:abc",
            "message": _message(message_id=20),
        },
    }
    normalized = normalize_callback(TelegramUpdate.model_validate(payload))
    assert normalized is not None
    assert normalized.callback_id == "cb-1"
    assert normalized.data == "feedback:start:abc"
    assert normalized.message_id == "20"
    assert normalized.sender_id != "111"


def test_webhook_secret_validation() -> None:
    """The webhook secret must match exactly."""
    assert is_valid_webhook_secret("s3cret", "s3cret") is True
    assert is_valid_webhook_secret("wrong", "s3cret") is False
    assert is_valid_webhook_secret(None, "s3cret") is False
    assert is_valid_webhook_secret("s3cret", "") is False


def test_ordinary_group_message_reports_its_sender_as_a_member() -> None:
    """Speaking in a served group is what makes someone a member of it."""
    normalized = normalize_message(_update(), IDENTITY)
    assert normalized is not None
    assert [
        (event.principal_id, event.change) for event in normalized.member_events
    ] == [("telegram:111", MemberChange.JOINED)]


def test_private_message_reports_no_membership() -> None:
    """A private chat is not a space, so it grants no membership."""
    message = _message(chat={"id": 111, "type": "private"})
    normalized = normalize_message(_update(message), IDENTITY)
    assert normalized is not None
    assert normalized.member_events == []


def test_join_service_message_reports_the_people_who_joined() -> None:
    """A join service message lists arrivals, bots included."""
    message = _message(
        text=None,
        new_chat_members=[
            {"id": 222, "is_bot": False},
            {"id": 999, "is_bot": True},
        ],
    )
    normalized = normalize_message(_update(message), IDENTITY)
    assert normalized is not None
    assert [
        (event.principal_id, event.change) for event in normalized.member_events
    ] == [("telegram:222", MemberChange.JOINED)]


def test_leave_service_message_reports_only_the_person_who_left() -> None:
    """Whoever removed someone is not evidence about themselves."""
    message = _message(
        text=None,
        left_chat_member={"id": 222, "is_bot": False},
    )
    normalized = normalize_message(_update(message), IDENTITY)
    assert normalized is not None
    assert [
        (event.principal_id, event.change) for event in normalized.member_events
    ] == [("telegram:222", MemberChange.LEFT)]


def test_anonymous_message_reports_no_membership() -> None:
    """A message with no identifiable sender cannot identify a member."""
    message = _message(text=None, **{"from": None})
    normalized = normalize_message(_update(message), IDENTITY)
    assert normalized is not None
    assert normalized.member_events == []
