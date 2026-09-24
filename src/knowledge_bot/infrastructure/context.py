# SPDX-License-Identifier: MIT
"""Shared application context exposed to transport adapters."""

from dataclasses import dataclass

from knowledge_bot.adapters.inbound.telegram import TelegramIdentity
from knowledge_bot.adapters.telegram.client import TelegramClient
from knowledge_bot.application.answer_question import AnswerService
from knowledge_bot.application.background import BackgroundIndexer
from knowledge_bot.application.budget import AiBudget
from knowledge_bot.application.classifier import MessageClassifier
from knowledge_bot.application.daily_report import DailyReportService
from knowledge_bot.application.feedback import FeedbackService
from knowledge_bot.application.groups import SpaceDirectory
from knowledge_bot.application.indexing import SearchProjectionService
from knowledge_bot.application.ingest import MessageIngestor
from knowledge_bot.application.interactions import InteractionService
from knowledge_bot.application.listener_pairing import MessagePairingService
from knowledge_bot.application.reindex import ReindexService
from knowledge_bot.application.revert import CorrectionReverter
from knowledge_bot.application.review import ReviewService
from knowledge_bot.application.reviewers import ReviewerManager, ReviewerRouter
from knowledge_bot.application.runtime_smoke import RuntimeSmokeService
from knowledge_bot.application.seed import SeedService
from knowledge_bot.infrastructure.settings import Settings
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.repositories import DeliveryReceiptRepository


@dataclass(frozen=True, slots=True)
class AppContext:
    """Services and adapter dependencies shared by HTTP routes."""

    settings: Settings
    identity: TelegramIdentity
    clock: Clock
    ingestor: MessageIngestor
    classifier: MessageClassifier
    background_indexer: BackgroundIndexer
    answer: AnswerService
    reindex: ReindexService
    seed: SeedService
    spaces: SpaceDirectory
    review: ReviewService
    feedback: FeedbackService
    interactions: InteractionService
    reviewers: ReviewerManager
    router: ReviewerRouter
    daily_report: DailyReportService
    reverter: CorrectionReverter
    budget: AiBudget
    transport: TelegramClient
    delivery_receipts: DeliveryReceiptRepository
    pairing: MessagePairingService
    projector: SearchProjectionService
    runtime_smoke: RuntimeSmokeService
