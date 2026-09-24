# SPDX-License-Identifier: MIT
"""Telegram-specific outbound client contract."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class TelegramClient(Protocol):
    """Operations owned by the Telegram connector."""

    async def send_message(self, conversation_id: str, text: str) -> str | None:
        """Send a plain Telegram message."""
        ...

    async def send_answer(
        self, conversation_id: str, text: str, answer_id: str
    ) -> str | None:
        """Send an answer with its feedback button."""
        ...

    async def send_review(
        self,
        conversation_id: str,
        text: str,
        feedback_id: str,
        include_global: bool = True,
    ) -> str | None:
        """Send a correction review."""
        ...

    async def send_force_reply(self, conversation_id: str, text: str) -> str | None:
        """Send a Telegram force-reply prompt."""
        ...

    async def edit_message(
        self, conversation_id: str, message_id: str, text: str
    ) -> bool:
        """Edit a Telegram message."""
        ...

    async def answer_callback(self, callback_id: str, alert: str | None = None) -> None:
        """Acknowledge a Telegram callback."""
        ...
