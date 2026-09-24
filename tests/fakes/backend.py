# SPDX-License-Identifier: MIT
"""Shared in-memory backend for integration tests."""

from dataclasses import dataclass, field

from tests.fakes.repositories import (
    InMemoryAttachmentRepository,
    InMemoryBotAnswerRepository,
    InMemoryChannelBindingRepository,
    InMemoryConversationRepository,
    InMemoryDeliveryReceiptRepository,
    InMemoryFeedbackRepository,
    InMemoryMessageRepository,
    InMemoryQAEvidenceRepository,
    InMemoryQAItemRepository,
    InMemoryQAVersionRepository,
    InMemoryReviewerEventRepository,
    InMemoryReviewerRepository,
    InMemorySourceRepository,
    InMemorySpaceRepository,
    InMemoryTelegramInteractionRepository,
)
from tests.fakes.support import (
    InMemoryAiUsageRepository,
    InMemoryRecapStateRepository,
    InMemoryReportStateRepository,
)


@dataclass(slots=True)
class InMemoryBackend:
    """Own one instance of every stateful test store."""

    sources: InMemorySourceRepository = field(default_factory=InMemorySourceRepository)
    spaces: InMemorySpaceRepository = field(default_factory=InMemorySpaceRepository)
    bindings: InMemoryChannelBindingRepository = field(
        default_factory=InMemoryChannelBindingRepository
    )
    conversations: InMemoryConversationRepository = field(
        default_factory=InMemoryConversationRepository
    )
    messages: InMemoryMessageRepository = field(
        default_factory=InMemoryMessageRepository
    )
    attachments: InMemoryAttachmentRepository = field(
        default_factory=InMemoryAttachmentRepository
    )
    answers: InMemoryBotAnswerRepository = field(
        default_factory=InMemoryBotAnswerRepository
    )
    feedback: InMemoryFeedbackRepository = field(
        default_factory=InMemoryFeedbackRepository
    )
    delivery_receipts: InMemoryDeliveryReceiptRepository = field(
        default_factory=InMemoryDeliveryReceiptRepository
    )
    telegram_interactions: InMemoryTelegramInteractionRepository = field(
        default_factory=InMemoryTelegramInteractionRepository
    )
    qa_items: InMemoryQAItemRepository = field(default_factory=InMemoryQAItemRepository)
    qa_versions: InMemoryQAVersionRepository = field(
        default_factory=InMemoryQAVersionRepository
    )
    qa_evidence: InMemoryQAEvidenceRepository = field(
        default_factory=InMemoryQAEvidenceRepository
    )
    reviewers: InMemoryReviewerRepository = field(
        default_factory=InMemoryReviewerRepository
    )
    reviewer_events: InMemoryReviewerEventRepository = field(
        default_factory=InMemoryReviewerEventRepository
    )
    recap_state: InMemoryRecapStateRepository = field(
        default_factory=InMemoryRecapStateRepository
    )
    report_state: InMemoryReportStateRepository = field(
        default_factory=InMemoryReportStateRepository
    )
    ai_usage: InMemoryAiUsageRepository = field(
        default_factory=InMemoryAiUsageRepository
    )
