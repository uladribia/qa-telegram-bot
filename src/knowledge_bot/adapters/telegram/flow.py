# SPDX-License-Identifier: MIT
"""Telegram connector flow boundary."""

from collections.abc import Awaitable, Callable

from knowledge_bot.contracts.telegram import TelegramUpdate
from knowledge_bot.infrastructure.context import AppContext

TelegramUpdateHandler = Callable[[AppContext, TelegramUpdate], Awaitable[str]]


class TelegramFlow:
    """Own the Telegram update-to-application dispatch boundary."""

    def __init__(self, handler: TelegramUpdateHandler) -> None:
        """Store the connector dispatcher."""
        self._handler = handler

    async def handle(self, context: AppContext, update: TelegramUpdate) -> str:
        """Dispatch one validated Telegram update."""
        return await self._handler(context, update)
