# SPDX-License-Identifier: MIT
"""Outbound transport port.

The core sends channel-agnostic messages; adapters map them to Telegram (v1),
email, or other channels later.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class MessageTransport(Protocol):
    """Sends and edits messages in a conversation."""

    async def send_message(self, conversation_id: str, text: str) -> str | None:
        """Send a text message and return the platform message id, if any."""
        ...

    async def send_answer(
        self,
        conversation_id: str,
        text: str,
        answer_id: str,
    ) -> str | None:
        """Send an answer carrying the correction affordance for an answer id."""
        ...

    async def edit_message(
        self,
        conversation_id: str,
        message_id: str,
        text: str,
    ) -> bool:
        """Replace the text of a message; return whether it succeeded."""
        ...

    async def send_force_reply(self, conversation_id: str, text: str) -> str | None:
        """Ask for a reply, returning the prompt message id for correlation."""
        ...

    async def answer_callback(self, callback_id: str) -> None:
        """Acknowledge an inline-button press."""
        ...
