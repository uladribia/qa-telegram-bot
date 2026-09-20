# SPDX-License-Identifier: MIT
"""In-memory repository fakes for tests (no database, no network).

The fakes are async to match the D1-backed implementations and the ports.
"""

from datetime import datetime

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


class InMemorySourceRepository:
    """Dict-backed implementation of ``SourceRepository``."""

    def __init__(self) -> None:
        """Create an empty repository."""
        self._items: dict[str, Source] = {}

    async def add(self, source: Source) -> None:
        """Persist a new source."""
        if source.id in self._items:
            message = f"source already exists: {source.id}"
            raise ValueError(message)
        self._items[source.id] = source

    async def get(self, source_id: str) -> Source | None:
        """Return a source by id, if present."""
        return self._items.get(source_id)

    async def save(self, source: Source) -> None:
        """Persist changes to an existing source."""
        if source.id not in self._items:
            message = f"unknown source: {source.id}"
            raise KeyError(message)
        self._items[source.id] = source


class InMemoryConversationRepository:
    """Dict-backed implementation of ``ConversationRepository``."""

    def __init__(self) -> None:
        """Create an empty repository."""
        self._items: dict[str, Conversation] = {}

    async def add(self, conversation: Conversation) -> None:
        """Persist a new conversation."""
        if conversation.id in self._items:
            message = f"conversation already exists: {conversation.id}"
            raise ValueError(message)
        self._items[conversation.id] = conversation

    async def get(self, conversation_id: str) -> Conversation | None:
        """Return a conversation by id, if present."""
        return self._items.get(conversation_id)


class InMemoryMessageRepository:
    """Dict-backed implementation of ``MessageRepository`` with idempotency."""

    def __init__(self) -> None:
        """Create an empty repository."""
        self._items: dict[str, Message] = {}
        self._external: dict[tuple[str, str], str] = {}

    async def add(self, message: Message) -> bool:
        """Persist a message; return ``False`` when it already exists."""
        if message.id in self._items:
            return False
        if message.external_id is not None:
            key = (message.source_id, message.external_id)
            if key in self._external:
                return False
            self._external[key] = message.id
        self._items[message.id] = message
        return True

    async def get(self, message_id: str) -> Message | None:
        """Return a message by id, if present."""
        return self._items.get(message_id)

    async def get_by_external_id(
        self, source_id: str, external_id: str
    ) -> Message | None:
        """Return a message by its idempotency key, if present."""
        message_id = self._external.get((source_id, external_id))
        return self._items.get(message_id) if message_id is not None else None


class InMemoryAttachmentRepository:
    """Dict-backed implementation of ``AttachmentRepository``."""

    def __init__(self) -> None:
        """Create an empty repository."""
        self._items: dict[str, Attachment] = {}

    async def add(self, attachment: Attachment) -> None:
        """Persist attachment metadata."""
        self._items[attachment.id] = attachment

    async def list_for_message(self, message_id: str) -> list[Attachment]:
        """Return the attachments of a message."""
        return [item for item in self._items.values() if item.message_id == message_id]


class InMemoryQAItemRepository:
    """Dict-backed implementation of ``QAItemRepository``."""

    def __init__(self) -> None:
        """Create an empty repository."""
        self._items: dict[str, QAItem] = {}

    async def add(self, item: QAItem) -> None:
        """Persist a new Q&A item."""
        if any(
            existing.canonical_key == item.canonical_key
            for existing in self._items.values()
        ):
            message = f"canonical key already exists: {item.canonical_key}"
            raise ValueError(message)
        self._items[item.id] = item

    async def get(self, qa_id: str) -> QAItem | None:
        """Return a Q&A item by id, if present."""
        return self._items.get(qa_id)

    async def get_by_canonical_key(self, canonical_key: str) -> QAItem | None:
        """Return a Q&A item by canonical key, if present."""
        for item in self._items.values():
            if item.canonical_key == canonical_key:
                return item
        return None

    async def save(self, item: QAItem) -> None:
        """Persist changes to an existing Q&A item."""
        if item.id not in self._items:
            message = f"unknown QA item: {item.id}"
            raise KeyError(message)
        self._items[item.id] = item


class InMemoryQAVersionRepository:
    """Dict-backed implementation of ``QAVersionRepository``."""

    def __init__(self) -> None:
        """Create an empty repository."""
        self._items: dict[str, QAVersion] = {}

    async def add(self, version: QAVersion) -> None:
        """Persist a new Q&A version."""
        if version.id in self._items:
            message = f"QA version already exists: {version.id}"
            raise ValueError(message)
        self._items[version.id] = version

    async def get(self, version_id: str) -> QAVersion | None:
        """Return a Q&A version by id, if present."""
        return self._items.get(version_id)


class InMemoryQAEvidenceRepository:
    """Dict-backed implementation of ``QAEvidenceRepository``."""

    def __init__(self) -> None:
        """Create an empty repository."""
        self._items: dict[tuple[str, str, str], QAEvidence] = {}

    async def add(self, evidence: QAEvidence) -> None:
        """Persist an evidence link."""
        key = (
            evidence.qa_version_id,
            evidence.evidence_type.value,
            evidence.evidence_id,
        )
        self._items[key] = evidence


class InMemoryBotAnswerRepository:
    """Dict-backed implementation of ``BotAnswerRepository``."""

    def __init__(self) -> None:
        """Create an empty repository."""
        self._items: dict[str, BotAnswer] = {}

    async def add(self, answer: BotAnswer) -> None:
        """Persist a bot answer."""
        self._items[answer.id] = answer

    async def get(self, answer_id: str) -> BotAnswer | None:
        """Return a bot answer by id, if present."""
        return self._items.get(answer_id)

    async def list_between(self, start: datetime, end: datetime) -> list[BotAnswer]:
        """Return the answers created in ``[start, end)``."""
        return [item for item in self._items.values() if start <= item.created_at < end]


class InMemoryFeedbackRepository:
    """Dict-backed implementation of ``FeedbackRepository``."""

    def __init__(self) -> None:
        """Create an empty repository."""
        self._items: dict[str, Feedback] = {}

    async def add(self, feedback: Feedback) -> None:
        """Persist a new correction proposal."""
        if feedback.id in self._items:
            message = f"feedback already exists: {feedback.id}"
            raise ValueError(message)
        self._items[feedback.id] = feedback

    async def get(self, feedback_id: str) -> Feedback | None:
        """Return a correction proposal by id, if present."""
        return self._items.get(feedback_id)

    async def save(self, feedback: Feedback) -> None:
        """Persist changes to an existing correction proposal."""
        if feedback.id not in self._items:
            message = f"unknown feedback: {feedback.id}"
            raise KeyError(message)
        self._items[feedback.id] = feedback

    async def find_by_proposal_prompt(self, message_id: str) -> Feedback | None:
        """Return the feedback awaiting a proposal reply to a prompt message."""
        for feedback in self._items.values():
            if feedback.proposal_prompt_message_id == message_id:
                return feedback
        return None

    async def find_by_edit_prompt(self, message_id: str) -> Feedback | None:
        """Return the feedback awaiting an admin edit reply to a prompt message."""
        for feedback in self._items.values():
            if feedback.edit_prompt_message_id == message_id:
                return feedback
        return None
