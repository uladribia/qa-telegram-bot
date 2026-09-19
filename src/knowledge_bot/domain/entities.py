# SPDX-License-Identifier: MIT
"""Domain entities (pure data, no framework or I/O dependencies)."""

from dataclasses import dataclass
from datetime import datetime

from knowledge_bot.domain.enums import (
    AnswerMode,
    ContentType,
    EvidenceType,
    FeedbackStatus,
    ProcessingStatus,
    QAOrigin,
    QAStatus,
    SourceType,
)


@dataclass(frozen=True, slots=True)
class Source:
    """A body of knowledge the bot can draw from."""

    id: str
    source_type: SourceType
    authority: int
    created_at: datetime
    external_ref: str | None = None
    title: str | None = None
    canonical_url: str | None = None
    is_mutable: bool = False


@dataclass(frozen=True, slots=True)
class Conversation:
    """A channel conversation (Telegram chat, imported chat, ...)."""

    id: str
    source_id: str
    created_at: datetime
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
    external_id: str | None = None
    sender_hash: str | None = None
    sender_name: str | None = None
    text: str | None = None
    reply_to_message_id: str | None = None


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
    current_version_id: str | None = None


@dataclass(frozen=True, slots=True)
class QAVersion:
    """An immutable answer version for a QA item."""

    id: str
    qa_id: str
    answer: str
    authority: int
    origin: QAOrigin
    created_at: datetime
    confidence: float | None = None
    created_by: str | None = None
    supersedes_version_id: str | None = None


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
    user_message_id: str | None = None
    telegram_bot_message_id: str | None = None
    confidence: float | None = None
    qa_version_id: str | None = None
    sources_json: str = "[]"


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
    proposed_answer: str | None = None
    admin_edited_answer: str | None = None
    proposal_prompt_message_id: str | None = None
    edit_prompt_message_id: str | None = None
    resolved_at: datetime | None = None
