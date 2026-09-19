# SPDX-License-Identifier: MIT
"""Tests for the WhatsApp export parser."""

from datetime import datetime

from knowledge_bot.adapters.inbound.whatsapp_export import parse_export
from knowledge_bot.domain.enums import ContentType, ProcessingStatus

ANDROID = "19/09/26, 09:32 - Ada: Hola!\n19/09/26, 09:33 - Bob: Quan entrenen?\n"

IOS = "[19/09/26, 09:32:12] Ada: Hola!\n[19/09/26, 09:32:20] Bob: Adeu\n"

IOS_12H = "[9/3/26, 2:29:14 PM] Ada: Hola!\n[9/3/26, 2:29:34 PM] Bob: Adeu\n"

IOS_SYSTEM = "[9/3/26, 2:29:14 PM] - Ada ha afegit Tu\n[9/3/26, 2:29:34 PM] Ada: Hola\n"


def test_android_format_is_parsed() -> None:
    """Android ``date, time - Author: text`` lines are parsed."""
    result = parse_export(ANDROID)
    assert result.stats.messages == 2
    assert result.stats.parse_failures == 0
    first = result.messages[0]
    assert first.text == "Hola!"
    assert first.content_type is ContentType.TEXT
    assert first.source_type == "whatsapp"
    assert first.timestamp == datetime(2026, 9, 19, 9, 32)


def test_ios_format_is_parsed() -> None:
    """IOS ``[date, time] Author: text`` lines are parsed."""
    result = parse_export(IOS)
    assert result.stats.messages == 2
    assert result.messages[1].text == "Adeu"
    assert result.messages[0].timestamp == datetime(2026, 9, 19, 9, 32, 12)


def test_ios_12_hour_format_with_single_digit_date_is_parsed() -> None:
    """The iOS export format with 1-digit dates and AM/PM is parsed."""
    result = parse_export(IOS_12H)
    assert result.stats.messages == 2
    assert result.stats.parse_failures == 0
    assert result.messages[0].timestamp == datetime(2026, 3, 9, 14, 29, 14)
    assert result.messages[0].text == "Hola!"


def test_ios_system_line_with_dash_is_not_a_message() -> None:
    """An author-less iOS system line is counted, not emitted."""
    result = parse_export(IOS_SYSTEM)
    assert result.stats.system_messages == 1
    assert result.stats.messages == 1
    assert result.stats.parse_failures == 0


def test_multiline_message_is_joined() -> None:
    """Continuation lines are appended to the previous message."""
    result = parse_export("19/09/26, 09:32 - Ada: Linia 1\nLinia 2\nLinia 3\n")
    assert result.messages[0].text == "Linia 1\nLinia 2\nLinia 3"


def test_media_placeholder_is_metadata_only() -> None:
    """A media placeholder yields an attachment and no text."""
    result = parse_export("19/09/26, 09:32 - Ada: image omitted\n")
    message = result.messages[0]
    assert message.content_type is ContentType.IMAGE
    assert message.text is None
    assert len(message.attachments) == 1
    attachment = message.attachments[0]
    assert attachment.kind == "image"
    assert attachment.processing_status is ProcessingStatus.UNPROCESSED


def test_media_file_name_is_detected() -> None:
    """A media file name is recognised and classified by extension."""
    result = parse_export("19/09/26, 09:32 - Ada: IMG-20260919-WA0003.jpg\n")
    message = result.messages[0]
    assert message.content_type is ContentType.IMAGE
    assert message.attachments[0].file_name == "IMG-20260919-WA0003.jpg"


def test_media_caption_is_kept_as_text() -> None:
    """Text next to a media placeholder is kept as the caption."""
    result = parse_export("19/09/26, 09:32 - Ada: IMG-20260919-WA0003.jpg mira això\n")
    message = result.messages[0]
    assert message.content_type is ContentType.IMAGE
    assert message.text == "mira això"
    assert message.attachments[0].file_name == "IMG-20260919-WA0003.jpg"


def test_generic_placeholder_is_unknown_kind() -> None:
    """A placeholder without a kind is recorded but not misclassified."""
    result = parse_export("19/09/26, 09:32 - Ada: <Multimedia omès>\n")
    message = result.messages[0]
    assert message.content_type is ContentType.UNKNOWN
    assert len(message.attachments) == 1


def test_system_messages_are_counted_but_not_emitted() -> None:
    """Author-less system lines are skipped and counted."""
    text = (
        "19/09/26, 09:32 - Els missatges i les trucades estan xifrats de punt a punt.\n"
        "19/09/26, 09:33 - Ada: Hola\n"
    )
    result = parse_export(text)
    assert result.stats.system_messages == 1
    assert result.stats.messages == 1
    assert len(result.messages) == 1
    assert result.messages[0].text == "Hola"


def test_emoji_and_urls_are_preserved() -> None:
    """Unicode and URLs survive untouched."""
    text = "19/09/26, 09:32 - Ada: Ho sento 😂 mira https://example.com/a?b=1\n"
    result = parse_export(text)
    body = result.messages[0].text or ""
    assert "😂" in body
    assert "https://example.com/a?b=1" in body


def test_senders_are_pseudonymized_and_stable() -> None:
    """Sender ids are stable hashes that do not contain the raw name."""
    first = parse_export(ANDROID)
    second = parse_export(ANDROID)
    assert first.messages[0].sender_id == second.messages[0].sender_id
    assert first.messages[0].sender_id != first.messages[1].sender_id
    assert first.messages[0].sender_id is not None
    assert "Ada" not in first.messages[0].sender_id


def test_message_ids_are_stable_for_idempotency() -> None:
    """The same export produces the same message ids."""
    assert parse_export(ANDROID).messages[0].id == parse_export(ANDROID).messages[0].id


def test_preamble_is_not_a_parse_failure() -> None:
    """The encryption notice before the first message is ignored."""
    text = (
        "Els missatges i les trucades estan xifrats de punt a punt.\n"
        "19/09/26, 09:32 - Ada: Hola\n"
    )
    result = parse_export(text)
    assert result.stats.parse_failures == 0
    assert result.stats.messages == 1


def test_unparseable_header_is_counted_as_failure() -> None:
    """A header-looking line with an impossible date is a parse failure."""
    result = parse_export("19/13/26, 09:32 - Ada: data impossible\n")
    assert result.stats.parse_failures == 1
    assert result.stats.messages == 0


def test_date_range_is_captured() -> None:
    """The first and last message timestamps are reported."""
    result = parse_export(ANDROID)
    assert result.stats.first_timestamp == datetime(2026, 9, 19, 9, 32)
    assert result.stats.last_timestamp == datetime(2026, 9, 19, 9, 33)
