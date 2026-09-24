# SPDX-License-Identifier: MIT
"""Domain entities (pure data, no framework or I/O dependencies)."""

from dataclasses import dataclass
from datetime import datetime

from knowledge_bot.domain.enums import (
    AnswerMode,
    ClassificationStatus,
    ContentType,
    EvidenceType,
    FeedbackStatus,
    IndexStatus,
    ProcessingStatus,
    QAStatus,
)
from knowledge_bot.domain.scope import GLOBAL_SCOPE


@dataclass(frozen=True, slots=True)
class Space:
    """A channel-independent logical community context."""

    id: str
    created_at: datetime
    title: str | None = None


@dataclass(frozen=True, slots=True)
class ChannelBinding:
    """An external channel conversation bound to a logical space."""

    channel: str
    external_conversation_id: str
    conversation_id: str
    space_id: str
    created_at: datetime
    title: str | None = None


@dataclass(frozen=True, slots=True)
class Source:
    """A body of knowledge the bot can draw from."""

    id: str
    source_type: str
    authority: int
    created_at: datetime
    external_ref: str | None = None
    title: str | None = None
    canonical_url: str | None = None
    is_mutable: bool = False
    scope_key: str = GLOBAL_SCOPE


@dataclass(frozen=True, slots=True)
class Conversation:
    """A channel conversation (Telegram chat, imported chat, ...)."""

    id: str
    source_id: str
    created_at: datetime
    space_id: str | None = None
    external_id: str | None = None
    title: str | None = None


@dataclass(frozen=True, slots=True)
class Message:
    """A normalized message stored for retrieval and evidence."""

    id: str
    source_id: str
    conversation_id: str
    content_type: ContentType
    sent_at: datetime
    created_at: datetime
    sender_is_admin: bool = False
    sender_authority: int | None = None
    external_id: str | None = None
    sender_hash: str | None = None
    sender_name: str | None = None
    text: str | None = None
    reply_to_message_id: str | None = None
    intent_label: str | None = None
    intent_score: float | None = None
    context_question: str | None = None
    classification_status: ClassificationStatus = ClassificationStatus.NOT_CLASSIFIED
    intent_scores_json: str | None = None
    index_status: IndexStatus = IndexStatus.NOT_INDEXED
    indexed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Attachment:
    """Metadata about a message attachment; binary content is never stored."""

    id: str
    message_id: str
    kind: str
    processing_status: ProcessingStatus
    created_at: datetime
    external_file_id: str | None = None
    external_unique_id: str | None = None
    file_name: str | None = None
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class QAItem:
    """A canonical question with a pointer to its current answer version."""

    id: str
    canonical_key: str
    canonical_question: str
    status: QAStatus
    created_at: datetime
    updated_at: datetime
    scope_key: str = GLOBAL_SCOPE
    current_version_id: str | None = None


@dataclass(frozen=True, slots=True)
class QAVersion:
    """An immutable answer version for a QA item.

    ``source_url`` is set when the answer comes from the web snapshot (and is
    the exact anchored URL). ``author`` is set when a human proposed the answer
    through the correction flow. Exactly one of the two identifies the origin
    of the text, so a citation never claims the wrong source.
    """

    id: str
    qa_id: str
    answer: str
    authority: int
    origin: str
    created_at: datetime
    confidence: float | None = None
    created_by: str | None = None
    supersedes_version_id: str | None = None
    source_url: str | None = None
    source_anchor: str | None = None
    author: str | None = None


@dataclass(frozen=True, slots=True)
class Reviewer:
    """A person nominated to confirm corrections for a scope.

    ``user_id`` is a raw Telegram user id used as a routing key; ``name`` is
    only for display. There is exactly one reviewer per scope (``global`` or
    a group chat id).
    """

    scope: str
    user_id: str
    name: str
    created_at: datetime
    nominated_by: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewerEvent:
    """One correction resolution by a reviewer, for the admin report.

    ``reviewer_user_id`` is a routing key; ``reviewer_name`` is what the admin
    report shows. ``action`` is ``approved``/``edited_approved``/``rejected``
    and ``approval_scope`` is ``global`` or the target group chat id.
    """

    feedback_id: str
    action: str
    created_at: datetime
    reviewer_user_id: str | None = None
    reviewer_name: str | None = None
    group_label: str | None = None
    question: str | None = None
    approval_scope: str | None = None
    id: int | None = None
    reported: bool = False


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """One successful channel delivery of an application object."""

    id: str
    object_type: str
    object_id: str
    channel: str
    external_conversation_id: str
    external_message_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TelegramInteraction:
    """Durable correlation for a Telegram reply interaction."""

    external_message_id: str
    interaction_type: str
    object_id: str
    created_at: datetime
    principal_id: str | None = None
    consumed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class QAEvidence:
    """A link between a Q&A version and a piece of evidence."""

    qa_version_id: str
    evidence_type: EvidenceType
    evidence_id: str


@dataclass(frozen=True, slots=True)
class BotAnswer:
    """A stored answer the bot produced for a user message."""

    id: str
    conversation_id: str
    question: str
    answer: str
    answer_mode: AnswerMode
    created_at: datetime
    space_id: str | None = None
    user_message_id: str | None = None
    telegram_bot_message_id: str | None = None
    request_id: str | None = None
    confidence: float | None = None
    qa_version_id: str | None = None
    sources_json: str = "[]"


@dataclass(frozen=True, slots=True)
class MessagePairCandidate:
    """A non-authoritative question-answer pair extracted from listener messages."""

    id: str
    conversation_id: str
    question_message_id: str
    answer_message_id: str
    confidence: float
    source: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ListenerPairingWindow:
    """Durable accumulation state for one conversation's listener window."""

    id: str
    conversation_id: str
    started_at: datetime
    last_message_at: datetime
    processed_at: datetime | None = None
    status: str = "pending"


@dataclass(frozen=True, slots=True)
class Feedback:
    """A correction proposed by a user and its review state.

    ``reporter_chat_id`` is a routing key used to open a private chat with the
    reporter; it is never displayed.
    """

    id: str
    bot_answer_id: str
    status: FeedbackStatus
    created_at: datetime
    qa_id: str | None = None
    reporter_hash: str | None = None
    reporter_chat_id: str | None = None
    reporter_name: str | None = None
    proposed_answer: str | None = None
    admin_edited_answer: str | None = None
    proposal_prompt_message_id: str | None = None
    edit_prompt_message_id: str | None = None
    proposed_at: datetime | None = None
    resolved_at: datetime | None = None
    reviewer_delivery_failed_at: datetime | None = None
    reviewer_escalated_at: datetime | None = None
    reviewer_destination: str | None = None
