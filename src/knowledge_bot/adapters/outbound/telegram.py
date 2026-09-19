# SPDX-License-Identifier: MIT
"""Telegram outbound transport."""

from knowledge_bot.ports.http import HttpClient


class TelegramTransport:
    """Send messages through the Telegram Bot API."""

    def __init__(
        self,
        http: HttpClient,
        bot_token: str,
        api_base: str = "https://api.telegram.org",
    ) -> None:
        """Create the transport.

        Args:
            http: The HTTP client used to call Telegram.
            bot_token: The Telegram bot token.
            api_base: Base URL of the Telegram Bot API.
        """
        self._http = http
        self._token = bot_token
        self._api_base = api_base

    async def send_message(self, conversation_id: str, text: str) -> None:
        """Send a text message to a conversation."""
        await self._http.post_json(
            f"{self._api_base}/bot{self._token}/sendMessage",
            {"chat_id": conversation_id, "text": text},
        )
