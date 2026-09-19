# SPDX-License-Identifier: MIT
"""In-memory recap state and test doubles for time and transport."""

from datetime import datetime


class InMemoryRecapStateRepository:
    """Dict-backed implementation of ``RecapStateRepository``."""

    def __init__(self) -> None:
        """Create an empty repository."""
        self._last_sent: dict[str, datetime] = {}

    async def get_last_sent_at(self, conversation_id: str) -> datetime | None:
        """Return when the last recap was sent, if ever."""
        return self._last_sent.get(conversation_id)

    async def set_last_sent_at(self, conversation_id: str, sent_at: datetime) -> None:
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
        self.answers: list[tuple[str, str, str]] = []
        self.edits: list[tuple[str, str, str]] = []
        self.force_replies: list[tuple[str, str]] = []
        self.reviews: list[tuple[str, str, str]] = []

    async def send_message(self, conversation_id: str, text: str) -> str | None:
        """Record a message."""
        self.messages.append((conversation_id, text))
        return str(len(self.messages))

    async def send_answer(
        self, conversation_id: str, text: str, answer_id: str
    ) -> str | None:
        """Record an answer with its feedback button target."""
        self.answers.append((conversation_id, text, answer_id))
        return str(len(self.answers))

    async def edit_message(
        self, conversation_id: str, message_id: str, text: str
    ) -> bool:
        """Record an edit."""
        self.edits.append((conversation_id, message_id, text))
        return True

    async def send_force_reply(self, conversation_id: str, text: str) -> str | None:
        """Record a force-reply prompt."""
        self.force_replies.append((conversation_id, text))
        return str(len(self.force_replies))

    async def send_review(
        self, conversation_id: str, text: str, feedback_id: str
    ) -> str | None:
        """Record an admin review message."""
        self.reviews.append((conversation_id, text, feedback_id))
        return str(len(self.reviews))

    async def answer_callback(self, callback_id: str) -> None:
        """Record a callback acknowledgement."""
        return
