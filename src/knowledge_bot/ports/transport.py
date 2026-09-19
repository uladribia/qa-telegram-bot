# SPDX-License-Identifier: MIT
"""Outbound transport port.

The core sends channel-agnostic messages; adapters map them to Telegram (v1),
email, or other channels later.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class MessageTransport(Protocol):
    """Sends messages to a conversation."""

    async def send_message(self, conversation_id: str, text: str) -> None:
        """Send a text message to a conversation."""
        ...
