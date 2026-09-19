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
