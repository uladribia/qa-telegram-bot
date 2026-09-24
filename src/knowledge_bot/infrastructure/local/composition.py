# SPDX-License-Identifier: MIT
"""Composition root for the fully local SQLite and Ollama runtime."""

from pathlib import Path

import httpx

from knowledge_bot.adapters.inbound.telegram import TelegramIdentity
from knowledge_bot.adapters.outbound.telegram import TelegramTransport
from knowledge_bot.application.answer_question import AnswerService
from knowledge_bot.application.background import BackgroundIndexer
from knowledge_bot.application.budget import AiBudget
from knowledge_bot.application.classifier import MessageClassifier
from knowledge_bot.application.daily_report import DailyReportService
from knowledge_bot.application.feedback import FeedbackService
from knowledge_bot.application.groups import SpaceDirectory
from knowledge_bot.application.ingest import MessageIngestor
from knowledge_bot.application.recap_service import RecapService
from knowledge_bot.application.reindex import ReindexService
from knowledge_bot.application.retrieval import RetrievalService
from knowledge_bot.application.revert import CorrectionReverter
from knowledge_bot.application.review import ReviewService
from knowledge_bot.application.reviewers import (
    ReviewerManager,
    ReviewerReportService,
    ReviewerRouter,
)
from knowledge_bot.application.seed import SeedService
from knowledge_bot.infrastructure.clock import SystemClock
from knowledge_bot.infrastructure.cloudflare.d1 import (
    D1AiUsageRepository,
    D1AttachmentRepository,
    D1BotAnswerRepository,
    D1ChannelBindingRepository,
    D1ConversationRepository,
    D1CorrectionCommitStore,
    D1DailyReportStateRepository,
    D1DeliveryReceiptRepository,
    D1FeedbackRepository,
    D1MessageRepository,
    D1QAItemRepository,
    D1QAVersionRepository,
    D1RecapStateRepository,
    D1ReportStateRepository,
    D1ReviewerEventRepository,
    D1ReviewerRepository,
    D1ReviewSource,
    D1SearchIndexSource,
    D1SearchProjectionRepository,
    D1SourceRepository,
    D1SpaceRepository,
    D1TelegramInteractionRepository,
)
from knowledge_bot.infrastructure.composition import AppContext
from knowledge_bot.infrastructure.local.database import SQLiteDatabase, apply_migrations
from knowledge_bot.infrastructure.local.http import HttpxClient
from knowledge_bot.infrastructure.local.ollama import OllamaEmbedder, OllamaGenerator
from knowledge_bot.infrastructure.local.sqlite_repositories import SQLiteBinding
from knowledge_bot.infrastructure.local.vector_store import NumpySqliteVectorStore
from knowledge_bot.infrastructure.metering import MeteredEmbedder, MeteredGenerator
from knowledge_bot.infrastructure.settings import Settings
from knowledge_bot.ports.transport import MessageTransport


class LocalTransport:
    """No-op local delivery for API and scheduled flows without Telegram."""

    async def send_message(self, conversation_id: str, text: str) -> str | None:
        """Pretend a local message was delivered."""
        del conversation_id, text
        return "local-message"

    async def send_answer(
        self, conversation_id: str, text: str, answer_id: str
    ) -> str | None:
        """Pretend a local answer was delivered."""
        del conversation_id, text, answer_id
        return "local-answer"

    async def edit_message(
        self, conversation_id: str, message_id: str, text: str
    ) -> bool:
        """Pretend a local message was edited."""
        del conversation_id, message_id, text
        return True

    async def send_review(
        self,
        conversation_id: str,
        text: str,
        feedback_id: str,
        include_global: bool = True,
    ) -> str | None:
        """Pretend a local review was delivered."""
        del conversation_id, text, feedback_id, include_global
        return "local-review"

    async def send_force_reply(self, conversation_id: str, text: str) -> str | None:
        """Pretend a local prompt was delivered."""
        del conversation_id, text
        return "local-prompt"

    async def answer_callback(self, callback_id: str, alert: str | None = None) -> None:
        """Pretend a local callback was acknowledged."""
        del callback_id, alert


def _transport(settings: Settings, client: httpx.AsyncClient) -> MessageTransport:
    """Select Telegram delivery only when its credentials are configured."""
    if settings.telegram_bot_token:
        return TelegramTransport(HttpxClient(client), settings.telegram_bot_token)
    return LocalTransport()


async def build_context(
    settings: Settings,
) -> tuple[AppContext, SQLiteDatabase, httpx.AsyncClient]:
    """Build one local graph sharing one SQLite connection and HTTP client."""
    database = await SQLiteDatabase.connect(settings.sqlite_path)
    await apply_migrations(database, Path.cwd())
    client = httpx.AsyncClient(timeout=httpx.Timeout(120.0))
    transport = _transport(settings, client)
    clock = SystemClock()
    binding = SQLiteBinding(database)
    budget = AiBudget(
        usage=D1AiUsageRepository(binding),
        clock=clock,
        daily_neurons=float("inf"),
    )
    embedder = MeteredEmbedder(
        OllamaEmbedder(client, settings.ollama_base_url, settings.embedding_model),
        budget,
    )
    generator = MeteredGenerator(
        OllamaGenerator(client, settings.ollama_base_url, settings.generation_model),
        budget,
    )
    vectors = NumpySqliteVectorStore(database)
    classifier = MessageClassifier(
        embedder=embedder,
        chitchat_discard_threshold=settings.classifier_chitchat_discard_threshold,
        keep_signal_threshold=settings.classifier_keep_signal_threshold,
        question_match_threshold=settings.classifier_question_match_threshold,
        answer_match_threshold=settings.classifier_answer_match_threshold,
    )
    answers = D1BotAnswerRepository(binding)
    messages = D1MessageRepository(binding)
    sources = D1SourceRepository(binding)
    conversations = D1ConversationRepository(binding)
    feedback = D1FeedbackRepository(binding)
    manifest = D1SearchProjectionRepository(binding)
    commits = D1CorrectionCommitStore(binding)
    recap = RecapService(
        answers=answers,
        conversations=conversations,
        state=D1RecapStateRepository(binding),
        transport=transport,
        clock=clock,
        admin_user_id=settings.admin_telegram_user_id or None,
        enabled=settings.recap_enabled,
        interval_hours=settings.recap_interval_hours,
        language=settings.recap_language,
        budget=budget,
        feedback=feedback,
        messages=messages,
    )
    reviewer_report = ReviewerReportService(
        events=D1ReviewerEventRepository(binding),
        state=D1ReportStateRepository(binding),
        transport=transport,
        clock=clock,
        admin_user_id=settings.admin_telegram_user_id,
        mode=settings.admin_report_mode,
        interval_min=settings.admin_report_interval_min,
        budget=budget,
    )
    context = AppContext(
        settings=settings,
        identity=TelegramIdentity(
            allowed_chat_ids=frozenset(settings.allowed_chat_ids),
            admin_user_id=settings.admin_telegram_user_id,
            bot_id=settings.telegram_bot_id,
            bot_username=settings.telegram_bot_username,
            allowed_user_ids=frozenset(
                item.strip()
                for item in settings.allowed_telegram_user_ids.split(",")
                if item.strip()
            ),
        ),
        clock=clock,
        ingestor=MessageIngestor(
            sources=sources,
            conversations=conversations,
            messages=messages,
            attachments=D1AttachmentRepository(binding),
        ),
        classifier=classifier,
        background_indexer=BackgroundIndexer(
            messages=messages,
            conversations=conversations,
            sources=sources,
            classifier=classifier,
            embedder=embedder,
            vectors=vectors,
            manifest=manifest,
            clock=clock,
            answer_threshold=settings.classifier_answer_match_threshold,
        ),
        answer=AnswerService(
            retrieval=RetrievalService(
                embedder=embedder,
                vectors=vectors,
                qa_top_k=settings.qa_top_k,
                message_top_k=settings.message_top_k,
            ),
            generator=generator,
            answers=answers,
            delivery_receipts=D1DeliveryReceiptRepository(binding),
            transport=transport,
            channel="local",
            clock=clock,
            direct_qa_threshold=settings.direct_qa_threshold,
            synthesis_threshold=settings.synthesis_threshold,
        ),
        recap=recap,
        reindex=ReindexService(
            source=D1SearchIndexSource(binding),
            embedder=embedder,
            vectors=vectors,
            manifest=manifest,
            clock=clock,
        ),
        seed=SeedService(
            qa_items=D1QAItemRepository(binding),
            qa_versions=D1QAVersionRepository(binding),
            sources=sources,
            ingestor=MessageIngestor(
                sources=sources,
                conversations=conversations,
                messages=messages,
                attachments=D1AttachmentRepository(binding),
            ),
            clock=clock,
        ),
        spaces=SpaceDirectory(
            sources=sources,
            conversations=conversations,
            spaces=D1SpaceRepository(binding),
            bindings=D1ChannelBindingRepository(binding),
            clock=clock,
        ),
        review=ReviewService(
            source=D1ReviewSource(binding),
            conversations=conversations,
        ),
        feedback=FeedbackService(
            answers=answers,
            feedback=feedback,
            qa_items=D1QAItemRepository(binding),
            qa_versions=D1QAVersionRepository(binding),
            conversations=conversations,
            commits=commits,
            clock=clock,
        ),
        feedback_repo=feedback,
        delivery_receipts=D1DeliveryReceiptRepository(binding),
        telegram_interactions=D1TelegramInteractionRepository(binding),
        reviewers=ReviewerManager(reviewers=D1ReviewerRepository(binding), clock=clock),
        router=ReviewerRouter(
            reviewers=D1ReviewerRepository(binding),
            admin_user_id=settings.admin_telegram_user_id,
        ),
        reviewer_report=reviewer_report,
        daily_report=DailyReportService(
            recap=recap,
            reviewer_report=reviewer_report,
            state=D1DailyReportStateRepository(binding),
            transport=transport,
            clock=clock,
            admin_principal_id=settings.admin_telegram_user_id,
        ),
        reverter=CorrectionReverter(
            qa_items=D1QAItemRepository(binding),
            qa_versions=D1QAVersionRepository(binding),
        ),
        budget=budget,
        transport=transport,
    )
    return context, database, client
