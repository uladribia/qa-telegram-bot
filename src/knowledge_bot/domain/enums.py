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
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class IntentLabel(StrEnum):
    """Semantic intent labels used by background classification."""

    QUESTION = "question"
    KNOWLEDGE_UPDATE = "knowledge_update"
    CORRECTION = "correction"
    CHITCHAT = "chitchat"


class ClassificationStatus(StrEnum):
    """Lifecycle of background message classification."""

    NOT_CLASSIFIED = "not_classified"
    NO_TEXT = "no_text"
    PREFILTER_CHITCHAT = "prefilter_chitchat"
    CLASSIFIED = "classified"
    DEFERRED_BUDGET = "deferred_budget"
    FAILED = "failed"


class IndexStatus(StrEnum):
    """Lifecycle of background evidence indexing."""

    NOT_INDEXED = "not_indexed"
    NOT_ELIGIBLE = "not_eligible"
    PENDING = "pending"
    INDEXED = "indexed"
    FAILED = "failed"


class ReviewAction(StrEnum):
    """Actions available in correction review."""

    APPROVE_LOCAL = "approve_local"
    APPROVE_GLOBAL = "approve_global"
    EDIT = "edit"
    REJECT = "reject"


class AiWorkClass(StrEnum):
    """Priority classes for estimated AI budget admission."""

    USER = "user"
    BACKGROUND = "background"
    MAINTENANCE = "maintenance"


class AnswerMode(StrEnum):
    """How the bot produced an answer (spec §16)."""

    DIRECT_QA = "direct_qa"
    SYNTHESIS = "synthesis"
    ABSTENTION = "abstention"
    UNAVAILABLE = "unavailable"
