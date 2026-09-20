# SPDX-License-Identifier: MIT
"""Repository ports.

Implemented by infrastructure adapters (D1 in production) and by the in-memory
fakes used in tests. All operations are asynchronous because D1 is.
"""

from datetime import datetime
from typing import Protocol, runtime_checkable

from knowledge_bot.domain.entities import (
    Attachment,
    BotAnswer,
    Conversation,
    Feedback,
    Message,
    QAEvidence,
    QAItem,
    QAVersion,
    Source,
)


@runtime_checkable
class SourceRepository(Protocol):
    """Persistence for knowledge sources."""

    async def add(self, source: Source) -> None:
        """Persist a new source."""
        ...

    async def get(self, source_id: str) -> Source | None:
        """Return a source by id, if present."""
        ...

    async def save(self, source: Source) -> None:
        """Persist changes to an existing source."""
        ...


@runtime_checkable
class ConversationRepository(Protocol):
    """Persistence for conversations."""

    async def add(self, conversation: Conversation) -> None:
        """Persist a new conversation."""
        ...

    async def get(self, conversation_id: str) -> Conversation | None:
        """Return a conversation by id, if present."""
        ...


@runtime_checkable
class MessageRepository(Protocol):
    """Persistence for messages, with idempotent insertion."""

    async def add(self, message: Message) -> bool:
        """Persist a message; return ``False`` when it already exists."""
        ...

    async def get(self, message_id: str) -> Message | None:
        """Return a message by id, if present."""
        ...

    async def get_by_external_id(
        self, source_id: str, external_id: str
    ) -> Message | None:
        """Return a message by its idempotency key, if present."""
        ...


@runtime_checkable
class AttachmentRepository(Protocol):
    """Persistence for attachment metadata."""

    async def add(self, attachment: Attachment) -> None:
        """Persist attachment metadata."""
        ...

    async def list_for_message(self, message_id: str) -> list[Attachment]:
        """Return the attachments of a message."""
        ...


@runtime_checkable
class QAItemRepository(Protocol):
    """Persistence for canonical Q&A items."""

    async def add(self, item: QAItem) -> None:
        """Persist a new Q&A item."""
        ...

    async def get(self, qa_id: str) -> QAItem | None:
        """Return a Q&A item by id, if present."""
        ...

    async def get_by_canonical_key(self, canonical_key: str) -> QAItem | None:
        """Return a Q&A item by canonical key, if present."""
        ...

    async def save(self, item: QAItem) -> None:
        """Persist changes to an existing Q&A item."""
        ...


@runtime_checkable
class QAVersionRepository(Protocol):
    """Persistence for immutable Q&A answer versions."""

    async def add(self, version: QAVersion) -> None:
        """Persist a new Q&A version."""
        ...

    async def get(self, version_id: str) -> QAVersion | None:
        """Return a Q&A version by id, if present."""
        ...


@runtime_checkable
class QAEvidenceRepository(Protocol):
    """Persistence for Q&A evidence links."""

    async def add(self, evidence: QAEvidence) -> None:
        """Persist an evidence link."""
        ...


@runtime_checkable
class BotAnswerRepository(Protocol):
    """Persistence for answers the bot produced."""

    async def add(self, answer: BotAnswer) -> None:
        """Persist a bot answer."""
        ...

    async def get(self, answer_id: str) -> BotAnswer | None:
        """Return a bot answer by id, if present."""
        ...

    async def list_between(self, start: datetime, end: datetime) -> list[BotAnswer]:
        """Return the answers created in ``[start, end)``."""
        ...


@runtime_checkable
class FeedbackRepository(Protocol):
    """Persistence for correction proposals."""

    async def add(self, feedback: Feedback) -> None:
        """Persist a new correction proposal."""
        ...

    async def get(self, feedback_id: str) -> Feedback | None:
        """Return a correction proposal by id, if present."""
        ...

    async def save(self, feedback: Feedback) -> None:
        """Persist changes to an existing correction proposal."""
        ...

    async def find_by_proposal_prompt(self, message_id: str) -> Feedback | None:
        """Return the feedback awaiting a proposal reply to a prompt message."""
        ...

    async def find_by_edit_prompt(self, message_id: str) -> Feedback | None:
        """Return the feedback awaiting an admin edit reply to a prompt message."""
        ...


@runtime_checkable
class RecapStateRepository(Protocol):
    """Persistence for the last time a recap was sent per conversation."""

    async def get_last_sent_at(self, conversation_id: str) -> datetime | None:
        """Return when the last recap was sent, if ever."""
        ...

    async def set_last_sent_at(self, conversation_id: str, sent_at: datetime) -> None:
        """Record when a recap was sent."""
        ...
