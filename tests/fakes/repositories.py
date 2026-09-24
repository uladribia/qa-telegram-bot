# SPDX-License-Identifier: MIT
"""In-memory repository fakes for tests (no database, no network).

The fakes are async to match the D1-backed implementations and the ports.
"""

from dataclasses import replace
from datetime import datetime

from knowledge_bot.domain.entities import (
    Attachment,
    BotAnswer,
    ChannelBinding,
    Conversation,
    DeliveryReceipt,
    Feedback,
    Message,
    MessagePairCandidate,
    QAEvidence,
    QAItem,
    QAVersion,
    Reviewer,
    ReviewerEvent,
    Source,
    Space,
    TelegramInteraction,
)
from knowledge_bot.domain.enums import FeedbackStatus
from knowledge_bot.domain.scope import GLOBAL_SCOPE


class InMemorySpaceRepository:
    """Dict-backed implementation of ``SpaceRepository``."""

    def __init__(self) -> None:
        """Create an empty repository."""
        self._items: dict[str, Space] = {}

    async def add(self, space: Space) -> None:
        """Persist a new space."""
        if space.id in self._items:
            message = f"space already exists: {space.id}"
            raise ValueError(message)
        self._items[space.id] = space

    async def get(self, space_id: str) -> Space | None:
        """Return a space by id, if present."""
        return self._items.get(space_id)


class InMemoryChannelBindingRepository:
    """Dict-backed implementation of ``ChannelBindingRepository``."""

    def __init__(self) -> None:
        """Create an empty repository."""
        self._items: dict[tuple[str, str], ChannelBinding] = {}

    async def get(
        self, channel: str, external_conversation_id: str
    ) -> ChannelBinding | None:
        """Return a binding, if present."""
        return self._items.get((channel, external_conversation_id))

    async def add(self, binding: ChannelBinding) -> None:
        """Persist a new binding."""
        key = (binding.channel, binding.external_conversation_id)
        if key in self._items:
            message = f"binding already exists: {key}"
            raise ValueError(message)
        self._items[key] = binding

    async def save(self, binding: ChannelBinding) -> None:
        """Persist changes to an existing binding."""
        key = (binding.channel, binding.external_conversation_id)
        if key not in self._items:
            message = f"unknown binding: {key}"
            raise KeyError(message)
        self._items[key] = binding


class InMemoryDeliveryReceiptRepository:
    """In-memory idempotent delivery receipts."""

    def __init__(self) -> None:
        """Create an empty repository."""
        self._items: dict[tuple[str, str, str], DeliveryReceipt] = {}

    async def get(
        self, object_type: str, object_id: str, channel: str
    ) -> DeliveryReceipt | None:
        """Return a prior successful delivery."""
        return self._items.get((object_type, object_id, channel))

    async def add(self, receipt: DeliveryReceipt) -> None:
        """Persist one successful delivery."""
        self._items[(receipt.object_type, receipt.object_id, receipt.channel)] = receipt


class InMemoryTelegramInteractionRepository:
    """In-memory durable Telegram reply interactions."""

    def __init__(self) -> None:
        """Create an empty repository."""
        self._items: dict[str, TelegramInteraction] = {}

    async def get(self, external_message_id: str) -> TelegramInteraction | None:
        """Return an interaction by prompt message id."""
        return self._items.get(external_message_id)

    async def add(self, interaction: TelegramInteraction) -> None:
        """Persist one prompt interaction."""
        self._items[interaction.external_message_id] = interaction

    async def consume(
        self,
        external_message_id: str,
        principal_id: str,
        consumed_at: datetime,
    ) -> TelegramInteraction | None:
        """Return and consume one unused interaction for its principal."""
        interaction = self._items.get(external_message_id)
        if (
            interaction is None
            or interaction.consumed_at is not None
            or (
                interaction.principal_id is not None
                and interaction.principal_id != principal_id
            )
        ):
            return None
        consumed = replace(interaction, consumed_at=consumed_at)
        self._items[external_message_id] = consumed
        return consumed


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

    async def save(self, conversation: Conversation) -> None:
        """Persist changes to an existing conversation."""
        if conversation.id not in self._items:
            message = f"conversation not found: {conversation.id}"
            raise ValueError(message)
        self._items[conversation.id] = conversation


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

    async def save(self, message: Message) -> None:
        """Persist classification and indexing state changes."""
        if message.id not in self._items:
            error = f"unknown message: {message.id}"
            raise KeyError(error)
        self._items[message.id] = message

    async def get_by_external_id(
        self, source_id: str, external_id: str
    ) -> Message | None:
        """Return a message by its idempotency key, if present."""
        message_id = self._external.get((source_id, external_id))
        return self._items.get(message_id) if message_id is not None else None

    async def list_by_classification_status(
        self, status: str, limit: int
    ) -> list[Message]:
        """Return a bounded batch with the requested classification state."""
        matching = sorted(
            (
                message
                for message in self._items.values()
                if message.classification_status.value == status
            ),
            key=lambda message: message.created_at,
        )
        return matching[:limit]

    async def list_recent(
        self, conversation_id: str, start: datetime, limit: int
    ) -> list[Message]:
        """Return recent messages in a conversation."""
        return sorted(
            [
                message
                for message in self._items.values()
                if message.conversation_id == conversation_id
                and message.created_at >= start
            ],
            key=lambda message: message.created_at,
        )[:limit]

    async def list_recent_unpaired_questions(
        self,
        conversation_id: str,
        since: datetime,
        until: datetime,
        limit: int,
    ) -> list[Message]:
        """Return recent unpaired question candidates, newest first."""
        return sorted(
            [
                message
                for message in self._items.values()
                if message.conversation_id == conversation_id
                and message.intent_label == "question"
                and message.context_question is None
                and message.text
                and since <= message.created_at <= until
            ],
            key=lambda message: message.created_at,
            reverse=True,
        )[:limit]

    async def listener_stats_between(
        self, start: datetime, end: datetime
    ) -> tuple[int, int]:
        """Return ``(ingested, paired)`` listener counts in ``[start, end)``."""
        ingested = paired = 0
        for message in self._items.values():
            if not start <= message.created_at < end:
                continue
            if message.intent_label is not None:
                ingested += 1
            if message.context_question is not None:
                paired += 1
        return (ingested, paired)


class InMemoryMessagePairCandidateRepository:
    """Dict-backed listener pairing candidates."""

    def __init__(self) -> None:
        """Create an empty repository."""
        self._items: dict[str, MessagePairCandidate] = {}

    async def add(self, candidate: MessagePairCandidate) -> bool:
        """Persist a candidate once."""
        if candidate.id in self._items:
            return False
        self._items[candidate.id] = candidate
        return True

    async def list_for_conversation(
        self, conversation_id: str
    ) -> list[MessagePairCandidate]:
        """Return candidates for one conversation."""
        return [
            candidate
            for candidate in self._items.values()
            if candidate.conversation_id == conversation_id
        ]


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
            and existing.scope_key == item.scope_key
            for existing in self._items.values()
        ):
            message = f"canonical key already exists: {item.canonical_key}"
            raise ValueError(message)
        self._items[item.id] = item

    async def get(self, qa_id: str) -> QAItem | None:
        """Return a Q&A item by id, if present."""
        return self._items.get(qa_id)

    async def get_by_canonical_key(
        self,
        canonical_key: str,
        scope_key: str = "global",
    ) -> QAItem | None:
        """Return a Q&A item by canonical key within a scope, if present."""
        for item in self._items.values():
            if item.canonical_key == canonical_key and item.scope_key == scope_key:
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

    async def get_by_request_id(self, request_id: str) -> BotAnswer | None:
        """Return the answer previously created for an idempotency key."""
        return next(
            (
                answer
                for answer in self._items.values()
                if answer.request_id == request_id
            ),
            None,
        )

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

    async def list_between(self, start: datetime, end: datetime) -> list[Feedback]:
        """Return the correction proposals created in ``[start, end)``."""
        return [
            feedback
            for feedback in self._items.values()
            if start <= feedback.created_at < end
        ]

    async def list_escalatable(self) -> list[Feedback]:
        """Return pending reviews whose reviewer delivery has failed."""
        return [
            feedback
            for feedback in self._items.values()
            if feedback.status is FeedbackStatus.PENDING_REVIEW
            and feedback.reviewer_delivery_failed_at is not None
            and feedback.reviewer_escalated_at is None
        ]


class InMemoryReviewerRepository:
    """Dict-backed implementation of ``ReviewerRepository``."""

    def __init__(self) -> None:
        """Create an empty repository."""
        self._items: dict[str, Reviewer] = {}

    async def get(self, scope: str) -> Reviewer | None:
        """Return the reviewer of a scope, if any."""
        return self._items.get(scope)

    async def save(self, reviewer: Reviewer) -> None:
        """Create or replace the reviewer of a scope."""
        self._items[reviewer.scope] = reviewer

    async def delete(self, scope: str) -> bool:
        """Remove the reviewer of a scope; return whether one existed."""
        return self._items.pop(scope, None) is not None

    async def all(self) -> list[Reviewer]:
        """Return every reviewer, global scope first."""
        return sorted(
            self._items.values(),
            key=lambda reviewer: (reviewer.scope != GLOBAL_SCOPE, reviewer.scope),
        )


class InMemoryReviewerEventRepository:
    """List-backed implementation of ``ReviewerEventRepository``."""

    def __init__(self) -> None:
        """Create an empty repository."""
        self._events: list[ReviewerEvent] = []

    async def add(self, event: ReviewerEvent) -> ReviewerEvent:
        """Persist an event and return it."""
        self._events.append(event)
        return event

    async def list_unreported(self) -> list[ReviewerEvent]:
        """Return events not yet included in an admin report."""
        return [event for event in self._events if not event.reported]

    async def mark_reported(self, feedback_ids: list[str]) -> None:
        """Mark the events of these feedback ids as reported."""
        self._events = [
            replace(event, reported=True)
            if event.feedback_id in feedback_ids
            else event
            for event in self._events
        ]
