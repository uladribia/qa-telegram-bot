# SPDX-License-Identifier: MIT
"""Tests for the Telegram outbound transport."""

from knowledge_bot.adapters.outbound.telegram import TelegramTransport
from tests.fakes.http import RecordingHttpClient


async def test_send_message_posts_to_telegram() -> None:
    """The transport posts sendMessage with the chat and text."""
    http = RecordingHttpClient()
    transport = TelegramTransport(http, "TOKEN")
    await transport.send_message("-100", "hola")
    assert http.calls == [
        (
            "https://api.telegram.org/botTOKEN/sendMessage",
            {"chat_id": "-100", "text": "hola"},
        )
    ]


async def test_telegram_ok_false_is_a_delivery_failure() -> None:
    """A Telegram-level error is not mistaken for a delivered message."""
    http = RecordingHttpClient()
    http.responses.append({"ok": False, "result": {"message_id": 99}})
    transport = TelegramTransport(http, "TOKEN")

    assert await transport.send_message("-100", "hola") is None

    http.responses.append({"ok": False, "result": {"message_id": 99}})
    assert await transport.edit_message("-100", "99", "editat") is False
