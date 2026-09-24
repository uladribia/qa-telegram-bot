# SPDX-License-Identifier: MIT
"""Domain enumerations shared by entities, policies, and ports."""

from enum import StrEnum


class ContentType(StrEnum):
    """Kind of message content."""

    TEXT = "text"
    IMAGE = "image"
    DOCUMENT = "document"
    AUDIO = "audio"
    VIDEO = "video"
    UNKNOWN = "unknown"


class ProcessingStatus(StrEnum):
    """Lifecycle of an attachment."""

    UNPROCESSED = "unprocessed"
    PROCESSED = "processed"
    IGNORED = "ignored"


class QAStatus(StrEnum):
    """Lifecycle of a canonical Q&A item."""

    ACTIVE = "active"
    UNDER_REVIEW = "under_review"
    SUPERSEDED = "superseded"


class EvidenceType(StrEnum):
    """Kind of record referenced as evidence."""

    MESSAGE = "message"
    WEB_QA = "web_qa"
    QA_VERSION = "qa_version"


class FeedbackStatus(StrEnum):
    """Lifecycle of a correction proposal."""

    AWAITING_PROPOSAL = "awaiting_proposal"
    PENDING_ADMIN = "pending_admin"
    APPROVED = "approved"
    REJECTED = "rejected"


class AnswerMode(StrEnum):
    """How the bot produced an answer (spec §16)."""

    DIRECT_QA = "direct_qa"
    SYNTHESIS = "synthesis"
    ABSTENTION = "abstention"
    UNAVAILABLE = "unavailable"
