# SPDX-License-Identifier: MIT
"""In-memory recap state and test doubles for time and transport."""

from datetime import datetime


class InMemoryRecapStateRepository:
    """Dict-backed implementation of ``RecapStateRepository``."""

    def __init__(self) -> None:
        """Create an empty repository."""
        self._last_sent: dict[str, datetime] = {}

    def get_last_sent_at(self, conversation_id: str) -> datetime | None:
        """Return when the last recap was sent, if ever."""
        return self._last_sent.get(conversation_id)

    def set_last_sent_at(self, conversation_id: str, sent_at: datetime) -> None:
        """Record when a recap was sent."""
        self._last_sent[conversation_id] = sent_at


class FrozenClock:
    """A clock frozen at a fixed instant, advanceable in tests."""

    def __init__(self, now: datetime) -> None:
        """Create a clock at ``now``."""
        self._now = now

    def now(self) -> datetime:
        """Return the current frozen time."""
        return self._now

    def advance_to(self, now: datetime) -> None:
        """Move the clock forward."""
        self._now = now


class RecordingTransport:
    """A transport that records sent messages instead of sending them."""

    def __init__(self) -> None:
        """Create an empty transport."""
        self.messages: list[tuple[str, str]] = []

    async def send_message(self, conversation_id: str, text: str) -> None:
        """Record a message."""
        self.messages.append((conversation_id, text))
