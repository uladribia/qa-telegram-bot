# SPDX-License-Identifier: MIT
"""Composition root for the fully local SQLite and Ollama runtime."""

from pathlib import Path

import httpx

from knowledge_bot.adapters.inbound.telegram import TelegramIdentity
from knowledge_bot.adapters.outbound.telegram import TelegramNotifier, TelegramTransport
from knowledge_bot.adapters.telegram.client import TelegramClient
from knowledge_bot.application.answer_policy import AnswerPolicy
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
from knowledge_bot.application.retrieval import RetrievalService
from knowledge_bot.application.revert import CorrectionReverter
from knowledge_bot.application.review import ReviewService
from knowledge_bot.application.reviewers import ReviewerManager, ReviewerRouter
from knowledge_bot.application.runtime_smoke import RuntimeSmokeService
from knowledge_bot.application.seed import SeedService
from knowledge_bot.infrastructure.classifier_head import load_classifier_head
from knowledge_bot.infrastructure.clock import SystemClock
from knowledge_bot.infrastructure.context import AppContext
from knowledge_bot.infrastructure.local.database import SQLiteDatabase, apply_migrations
from knowledge_bot.infrastructure.local.http import HttpxClient
from knowledge_bot.infrastructure.local.ollama import (
    OllamaEmbedder,
    OllamaGenerator,
)
from knowledge_bot.infrastructure.local.sqlite_repositories import SQLiteBinding
from knowledge_bot.infrastructure.local.vector_store import NumpySqliteVectorStore
from knowledge_bot.infrastructure.metering import (
    MeteredEmbedder,
    MeteredGenerator,
)
from knowledge_bot.infrastructure.settings import Settings
from knowledge_bot.infrastructure.sql.repositories import (
    SqlAiUsageRepository,
    SqlAttachmentRepository,
    SqlBotAnswerRepository,
    SqlChannelBindingRepository,
    SqlConversationRepository,
    SqlCorrectionCommitStore,
    SqlDailyReportSource,
    SqlDailyReportStateRepository,
    SqlDeliveryReceiptRepository,
    SqlFeedbackRepository,
    SqlLexicalIndex,
    SqlMessagePairCandidateRepository,
    SqlMessageRepository,
    SqlQAItemRepository,
    SqlQAVersionRepository,
    SqlReviewerRepository,
    SqlReviewSource,
    SqlSearchIndexSource,
    SqlSearchProjectionRepository,
    SqlSourceRepository,
    SqlSpaceRepository,
    SqlTelegramInteractionRepository,
)
from knowledge_bot.ports.clock import Clock

D1AiUsageRepository = SqlAiUsageRepository
D1AttachmentRepository = SqlAttachmentRepository
D1BotAnswerRepository = SqlBotAnswerRepository
D1ChannelBindingRepository = SqlChannelBindingRepository
D1ConversationRepository = SqlConversationRepository
D1CorrectionCommitStore = SqlCorrectionCommitStore
D1DailyReportSource = SqlDailyReportSource
D1DailyReportStateRepository = SqlDailyReportStateRepository
D1DeliveryReceiptRepository = SqlDeliveryReceiptRepository
D1FeedbackRepository = SqlFeedbackRepository
D1LexicalIndex = SqlLexicalIndex
D1MessagePairCandidateRepository = SqlMessagePairCandidateRepository
D1MessageRepository = SqlMessageRepository
D1QAItemRepository = SqlQAItemRepository
D1QAVersionRepository = SqlQAVersionRepository
D1ReviewerRepository = SqlReviewerRepository
D1ReviewSource = SqlReviewSource
D1SearchIndexSource = SqlSearchIndexSource
D1SearchProjectionRepository = SqlSearchProjectionRepository
D1SourceRepository = SqlSourceRepository
D1SpaceRepository = SqlSpaceRepository
D1TelegramInteractionRepository = SqlTelegramInteractionRepository


class LocalNotifier:
    """Local notifier that accepts text without an external provider."""

    async def send_text(self, principal_id: str, text: str) -> bool:
        """Pretend local delivery succeeded."""
        del principal_id, text
        return True


class LocalTransport:
    """No-op local delivery for API and scheduled flows without Telegram."""

    async def send_message(self, conversation_id: str, text: str) -> str | None:
        """Pretend a local message was delivered."""
        del conversation_id, text
        return "local-message"

    async def send_text(self, conversation_id: str, text: str) -> str | None:
        """Pretend a local text notification was delivered."""
        return await self.send_message(conversation_id, text)

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


def _transport(settings: Settings, client: httpx.AsyncClient) -> TelegramClient:
    """Select Telegram delivery only when its credentials are configured."""
    if settings.telegram_bot_token:
        return TelegramTransport(HttpxClient(client), settings.telegram_bot_token)
    return LocalTransport()


async def build_context(
    settings: Settings,
    *,
    transport: TelegramClient | None = None,
    clock: Clock | None = None,
) -> tuple[AppContext, SQLiteDatabase, httpx.AsyncClient]:
    """Build one local graph sharing one SQLite connection and HTTP client."""
    database = await SQLiteDatabase.connect(settings.sqlite_path)
    await apply_migrations(database, Path.cwd())
    client = httpx.AsyncClient(timeout=httpx.Timeout(120.0))
    transport = transport or _transport(settings, client)
    notifier = (
        TelegramNotifier(transport)
        if isinstance(transport, TelegramTransport)
        else LocalNotifier()
    )
    clock = clock or SystemClock()
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
        head=load_classifier_head(settings.classifier_model_path),
        confidence_threshold=settings.classifier_confidence_threshold,
        margin_threshold=settings.classifier_margin_threshold,
    )
    answers = D1BotAnswerRepository(binding)
    messages = D1MessageRepository(binding)
    sources = D1SourceRepository(binding)
    conversations = D1ConversationRepository(binding)
    feedback = D1FeedbackRepository(binding)
    manifest = D1SearchProjectionRepository(binding)
    lexical = D1LexicalIndex(binding)
    projector = SearchProjectionService(
        source=D1SearchIndexSource(binding),
        embedder=embedder,
        vectors=vectors,
        lexical=lexical,
        manifest=manifest,
        clock=clock,
        budget=budget,
    )
    runtime_smoke = RuntimeSmokeService(embedder, generator, vectors, manifest, clock)
    pair_candidates = D1MessagePairCandidateRepository(binding)
    commits = D1CorrectionCommitStore(binding)
    context = AppContext(
        settings=settings,
        identity=TelegramIdentity(
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
            projector=projector,
            clock=clock,
            confidence_threshold=settings.classifier_confidence_threshold,
            margin_threshold=settings.classifier_margin_threshold,
            budget=budget,
        ),
        answer=AnswerService(
            retrieval=RetrievalService(
                embedder=embedder,
                vectors=vectors,
                lexical=lexical,
                qa_top_k=settings.qa_top_k,
                message_top_k=settings.message_top_k,
            ),
            generator=generator,
            answers=answers,
            clock=clock,
            policy=AnswerPolicy(floor=settings.answer_similarity_floor),
            conversations=conversations,
            sources=sources,
        ),
        reindex=ReindexService(
            source=D1SearchIndexSource(binding),
            projector=projector,
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
        interactions=InteractionService(D1TelegramInteractionRepository(binding)),
        reviewers=ReviewerManager(reviewers=D1ReviewerRepository(binding), clock=clock),
        router=ReviewerRouter(
            reviewers=D1ReviewerRepository(binding),
            admin_principal_id=f"telegram:{settings.admin_telegram_user_id}",
        ),
        daily_report=DailyReportService(
            source=D1DailyReportSource(binding),
            state=D1DailyReportStateRepository(binding),
            notifier=notifier,
            budget=budget,
            clock=clock,
            admin_principal_id=f"telegram:{settings.admin_telegram_user_id}",
        ),
        reverter=CorrectionReverter(
            qa_items=D1QAItemRepository(binding),
            qa_versions=D1QAVersionRepository(binding),
        ),
        budget=budget,
        transport=transport,
        delivery_receipts=D1DeliveryReceiptRepository(binding),
        projector=projector,
        runtime_smoke=runtime_smoke,
        pairing=MessagePairingService(
            messages=messages,
            conversations=conversations,
            sources=sources,
            candidates=pair_candidates,
            projector=projector,
            clock=clock,
            budget=budget,
            question_window_minutes=settings.pairing_question_window_minutes,
            max_pending_questions=settings.pairing_max_pending_questions,
            confidence_threshold=settings.classifier_confidence_threshold,
            margin_threshold=settings.classifier_margin_threshold,
        ),
    )
    return context, database, client
