# SPDX-License-Identifier: MIT
"""Neutral names for SQL repository implementations."""

from knowledge_bot.infrastructure.cloudflare.d1 import (
    D1AiUsageRepository as SqlAiUsageRepository,
)
from knowledge_bot.infrastructure.cloudflare.d1 import (
    D1AttachmentRepository as SqlAttachmentRepository,
)
from knowledge_bot.infrastructure.cloudflare.d1 import (
    D1BotAnswerRepository as SqlBotAnswerRepository,
)
from knowledge_bot.infrastructure.cloudflare.d1 import (
    D1ChannelBindingRepository as SqlChannelBindingRepository,
)
from knowledge_bot.infrastructure.cloudflare.d1 import (
    D1ConversationRepository as SqlConversationRepository,
)
from knowledge_bot.infrastructure.cloudflare.d1 import (
    D1CorrectionCommitStore as SqlCorrectionCommitStore,
)
from knowledge_bot.infrastructure.cloudflare.d1 import (
    D1DailyReportSource as SqlDailyReportSource,
)
from knowledge_bot.infrastructure.cloudflare.d1 import (
    D1DailyReportStateRepository as SqlDailyReportStateRepository,
)
from knowledge_bot.infrastructure.cloudflare.d1 import (
    D1DeliveryReceiptRepository as SqlDeliveryReceiptRepository,
)
from knowledge_bot.infrastructure.cloudflare.d1 import (
    D1FeedbackRepository as SqlFeedbackRepository,
)
from knowledge_bot.infrastructure.cloudflare.d1 import (
    D1MessagePairCandidateRepository as SqlMessagePairCandidateRepository,
)
from knowledge_bot.infrastructure.cloudflare.d1 import (
    D1MessageRepository as SqlMessageRepository,
)
from knowledge_bot.infrastructure.cloudflare.d1 import (
    D1QAItemRepository as SqlQAItemRepository,
)
from knowledge_bot.infrastructure.cloudflare.d1 import (
    D1QAVersionRepository as SqlQAVersionRepository,
)
from knowledge_bot.infrastructure.cloudflare.d1 import (
    D1ReviewerRepository as SqlReviewerRepository,
)
from knowledge_bot.infrastructure.cloudflare.d1 import (
    D1ReviewSource as SqlReviewSource,
)
from knowledge_bot.infrastructure.cloudflare.d1 import (
    D1SearchIndexSource as SqlSearchIndexSource,
)
from knowledge_bot.infrastructure.cloudflare.d1 import (
    D1SearchProjectionRepository as SqlSearchProjectionRepository,
)
from knowledge_bot.infrastructure.cloudflare.d1 import (
    D1SourceRepository as SqlSourceRepository,
)
from knowledge_bot.infrastructure.cloudflare.d1 import (
    D1SpaceRepository as SqlSpaceRepository,
)
from knowledge_bot.infrastructure.cloudflare.d1 import (
    D1TelegramInteractionRepository as SqlTelegramInteractionRepository,
)

__all__ = [
    "SqlAiUsageRepository",
    "SqlAttachmentRepository",
    "SqlBotAnswerRepository",
    "SqlChannelBindingRepository",
    "SqlConversationRepository",
    "SqlCorrectionCommitStore",
    "SqlDailyReportSource",
    "SqlDailyReportStateRepository",
    "SqlDeliveryReceiptRepository",
    "SqlFeedbackRepository",
    "SqlMessagePairCandidateRepository",
    "SqlMessageRepository",
    "SqlQAItemRepository",
    "SqlQAVersionRepository",
    "SqlReviewSource",
    "SqlReviewerRepository",
    "SqlSearchIndexSource",
    "SqlSearchProjectionRepository",
    "SqlSourceRepository",
    "SqlSpaceRepository",
    "SqlTelegramInteractionRepository",
]
