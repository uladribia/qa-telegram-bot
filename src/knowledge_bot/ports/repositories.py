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
    ChannelBinding,
    Conversation,
    DeliveryReceipt,
    Feedback,
    ListenerPairingWindow,
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
from knowledge_bot.domain.scope import GLOBAL_SCOPE, Scope


@runtime_checkable
class SpaceRepository(Protocol):
    """Persistence for logical spaces."""

    async def add(self, space: Space) -> None:
        """Persist a new space."""
        ...

    async def get(self, space_id: str) -> Space | None:
        """Return a space by id, if present."""
        ...


@runtime_checkable
class ChannelBindingRepository(Protocol):
    """Persistence for external channel bindings."""

    async def get(
        self, channel: str, external_conversation_id: str
    ) -> ChannelBinding | None:
        """Return a binding, if present."""
        ...

    async def add(self, binding: ChannelBinding) -> None:
        """Persist a new binding."""
        ...

    async def save(self, binding: ChannelBinding) -> None:
        """Persist changes to an existing binding."""
        ...


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

    async def save(self, conversation: Conversation) -> None:
        """Persist changes to an existing conversation."""
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

    async def save(self, message: Message) -> None:
        """Persist classification and indexing state changes."""
        ...

    async def get_by_external_id(
        self, source_id: str, external_id: str
    ) -> Message | None:
        """Return a message by its idempotency key, if present."""
        ...

    async def list_by_classification_status(
        self, status: str, limit: int
    ) -> list[Message]:
        """Return a bounded batch with the requested classification state."""
        ...

    async def listener_stats_between(
        self, start: datetime, end: datetime
    ) -> tuple[int, int]:
        """Return ``(ingested, paired)`` listener counts in ``[start, end)``.

        Only messages classified by the background listener count as
        ingested; ``paired`` counts those matched to a parent question.
        """
        ...

    async def list_recent(
        self, conversation_id: str, start: datetime, limit: int
    ) -> list[Message]:
        """Return recent messages in a conversation ordered by creation time."""
        ...


@runtime_checkable
class ListenerPairingWindowRepository(Protocol):
    """Persistence for event-driven listener pairing windows."""

    async def get(self, conversation_id: str) -> ListenerPairingWindow | None:
        """Return the current window for a conversation."""
        ...

    async def save(self, window: ListenerPairingWindow) -> None:
        """Create or update a conversation window."""
        ...

    async def list_due(self, before: datetime) -> list[ListenerPairingWindow]:
        """Return pending windows whose quiet period has elapsed."""
        ...


@runtime_checkable
class MessagePairCandidateRepository(Protocol):
    """Persistence for non-authoritative listener pairing candidates."""

    async def add(self, candidate: MessagePairCandidate) -> bool:
        """Persist one candidate, returning false when already present."""
        ...

    async def list_for_conversation(
        self, conversation_id: str
    ) -> list[MessagePairCandidate]:
        """Return candidates for one conversation."""
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

    async def get_by_canonical_key(
        self,
        canonical_key: str,
        scope_key: Scope = GLOBAL_SCOPE,
    ) -> QAItem | None:
        """Return a Q&A item by canonical key within a scope, if present."""
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

    async def get_by_request_id(self, request_id: str) -> BotAnswer | None:
        """Return the answer previously created for an idempotency key."""
        ...

    async def list_between(self, start: datetime, end: datetime) -> list[BotAnswer]:
        """Return the answers created in ``[start, end)``."""
        ...


@runtime_checkable
class DeliveryReceiptRepository(Protocol):
    """Persistence for idempotent channel delivery."""

    async def get(
        self, object_type: str, object_id: str, channel: str
    ) -> DeliveryReceipt | None:
        """Return a prior successful delivery, if present."""
        ...

    async def add(self, receipt: DeliveryReceipt) -> None:
        """Persist one successful delivery."""
        ...


@runtime_checkable
class TelegramInteractionRepository(Protocol):
    """Persistence for connector reply interactions."""

    async def get(self, external_message_id: str) -> TelegramInteraction | None:
        """Return an interaction by prompt message id."""
        ...

    async def add(self, interaction: TelegramInteraction) -> None:
        """Persist one prompt interaction."""
        ...

    async def consume(
        self, external_message_id: str, consumed_at: datetime
    ) -> TelegramInteraction | None:
        """Atomically return and consume one unused interaction."""
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

    async def list_between(self, start: datetime, end: datetime) -> list[Feedback]:
        """Return the correction proposals created in ``[start, end)``."""
        ...

    async def list_escalatable(self) -> list[Feedback]:
        """Return pending reviews whose reviewer delivery has failed."""
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


@runtime_checkable
class ReviewerRepository(Protocol):
    """Persistence for correction reviewers, one per scope."""

    async def get(self, scope: str) -> Reviewer | None:
        """Return the reviewer of a scope, if any."""
        ...

    async def save(self, reviewer: Reviewer) -> None:
        """Create or replace the reviewer of a scope."""
        ...

    async def delete(self, scope: str) -> bool:
        """Remove the reviewer of a scope; return whether one existed."""
        ...

    async def all(self) -> list[Reviewer]:
        """Return every reviewer, global scope first."""
        ...


@runtime_checkable
class ReviewerEventRepository(Protocol):
    """Persistence for reviewer correction resolutions (admin report)."""

    async def add(self, event: ReviewerEvent) -> ReviewerEvent:
        """Persist an event and return it with its id."""
        ...

    async def list_unreported(self) -> list[ReviewerEvent]:
        """Return events not yet included in an admin report."""
        ...

    async def mark_reported(self, feedback_ids: list[str]) -> None:
        """Mark the events of these feedback ids as reported."""
        ...


@runtime_checkable
class DailyReportStateRepository(Protocol):
    """Persistence for the last successful scheduled daily report."""

    async def get(self, key: str) -> datetime | None:
        """Return the last successful send time for a report key."""
        ...

    async def set(self, key: str, sent_at: datetime) -> None:
        """Record a successful report send."""
        ...


@runtime_checkable
class ReportStateRepository(Protocol):
    """Persistence for the last time the admin report was sent."""

    async def get_last_sent_at(self) -> datetime | None:
        """Return when the last admin report was sent, if ever."""
        ...

    async def set_last_sent_at(self, sent_at: datetime) -> None:
        """Record when the admin report was sent."""
        ...
