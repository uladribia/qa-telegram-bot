# SPDX-License-Identifier: MIT
"""Compose the application context from Cloudflare Worker bindings."""

from typing import Protocol

from knowledge_bot.adapters.inbound.telegram import TelegramIdentity
from knowledge_bot.adapters.outbound.telegram import TelegramNotifier, TelegramTransport
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
from knowledge_bot.application.reviewers import (
    ReviewerManager,
    ReviewerRouter,
)
from knowledge_bot.application.runtime_smoke import RuntimeSmokeService
from knowledge_bot.application.seed import SeedService
from knowledge_bot.infrastructure.classifier_head import load_classifier_head
from knowledge_bot.infrastructure.clock import SystemClock
from knowledge_bot.infrastructure.cloudflare.d1 import (
    D1AiUsageRepository,
    D1AttachmentRepository,
    D1BotAnswerRepository,
    D1ChannelBindingRepository,
    D1ConversationRepository,
    D1CorrectionCommitStore,
    D1DailyReportSource,
    D1DailyReportStateRepository,
    D1Database,
    D1DeliveryReceiptRepository,
    D1FeedbackRepository,
    D1LexicalIndex,
    D1MessagePairCandidateRepository,
    D1MessageRepository,
    D1QAItemRepository,
    D1QAVersionRepository,
    D1ReviewerRepository,
    D1ReviewSource,
    D1SearchIndexSource,
    D1SearchProjectionRepository,
    D1SourceRepository,
    D1SpaceRepository,
    D1TelegramInteractionRepository,
)
from knowledge_bot.infrastructure.cloudflare.http import WorkersHttpClient
from knowledge_bot.infrastructure.cloudflare.vectorize import (
    VectorizeIndex,
    VectorizeStore,
)
from knowledge_bot.infrastructure.cloudflare.workers_ai import (
    AiRunner,
    WorkersAIEmbedder,
    WorkersAIGenerator,
)
from knowledge_bot.infrastructure.context import AppContext
from knowledge_bot.infrastructure.metering import (
    MeteredEmbedder,
    MeteredGenerator,
)
from knowledge_bot.infrastructure.settings import Settings


class WorkerEnv(Protocol):
    """The Cloudflare ``env`` object: bindings plus optional string variables."""

    DB: D1Database
    AI: AiRunner
    VECTORIZE: VectorizeIndex


def _text(env: WorkerEnv, name: str, default: str = "") -> str:
    value = getattr(env, name, None)
    return default if value is None else str(value)


def _ids(value: str) -> frozenset[str]:
    """Parse a comma-separated id list into a set of stripped ids."""
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def build_context(env: WorkerEnv) -> AppContext:
    """Build the application context from Worker bindings.

    Args:
        env: The Cloudflare ``env`` object with bindings and variables.

    Returns:
        The wired application context.
    """
    settings = Settings(
        telegram_bot_token=_text(env, "TELEGRAM_BOT_TOKEN"),
        telegram_webhook_secret=_text(env, "TELEGRAM_WEBHOOK_SECRET"),
        allowed_telegram_user_ids=_text(env, "ALLOWED_TELEGRAM_USER_IDS"),
        admin_telegram_user_id=_text(env, "ADMIN_TELEGRAM_USER_ID"),
        telegram_bot_id=_text(env, "TELEGRAM_BOT_ID"),
        telegram_bot_username=_text(env, "TELEGRAM_BOT_USERNAME"),
        internal_admin_key=_text(env, "INTERNAL_ADMIN_KEY"),
        reviewer_escalation_timeout_seconds=_text(
            env, "REVIEWER_ESCALATION_TIMEOUT_SECONDS", "86400"
        ),
        pairing_question_window_minutes=_text(
            env, "PAIRING_QUESTION_WINDOW_MINUTES", "5"
        ),
        pairing_max_pending_questions=_text(env, "PAIRING_MAX_PENDING_QUESTIONS", "5"),
        background_listener_enabled=_text(env, "BACKGROUND_LISTENER_ENABLED", "false"),
        classifier_confidence_threshold=_text(env, "CLASSIFIER_CONFIDENCE", "0.60"),
        classifier_margin_threshold=_text(env, "CLASSIFIER_MARGIN", "0.15"),
        classifier_model_path=_text(
            env, "CLASSIFIER_MODEL_PATH", "data/classifier/model.json"
        ),
        direct_qa_threshold=_text(env, "DIRECT_QA_THRESHOLD", "0.7"),
        synthesis_threshold=_text(env, "SYNTHESIS_THRESHOLD", "0.3"),
        qa_top_k=_text(env, "QA_TOP_K", "5"),
        message_top_k=_text(env, "MESSAGE_TOP_K", "4"),
        ai_daily_neuron_budget=_text(env, "AI_DAILY_NEURON_BUDGET", "10000"),
        ai_neuron_reserve_fraction=_text(env, "AI_NEURON_RESERVE_FRACTION", "0.25"),
        ai_background_budget_fraction=_text(
            env, "AI_BACKGROUND_BUDGET_FRACTION", "0.50"
        ),
        ai_maintenance_budget_fraction=_text(
            env, "AI_MAINTENANCE_BUDGET_FRACTION", "0.70"
        ),
        ai_embed_neurons_per_char=_text(env, "AI_EMBED_NEURONS_PER_CHAR", "0.015"),
        ai_chat_neurons_per_char=_text(env, "AI_CHAT_NEURONS_PER_CHAR", "0.020"),
        ai_embed_timeout_seconds=_text(env, "AI_EMBED_TIMEOUT_SECONDS", "10"),
        ai_generation_timeout_seconds=_text(env, "AI_GENERATION_TIMEOUT_SECONDS", "35"),
    )
    database = env.DB
    answers = D1BotAnswerRepository(database)
    transport = TelegramTransport(WorkersHttpClient(), settings.telegram_bot_token)
    notifier = TelegramNotifier(transport)
    clock = SystemClock()
    budget = AiBudget(
        usage=D1AiUsageRepository(database),
        clock=clock,
        daily_neurons=settings.ai_daily_neuron_budget,
        reserve_fraction=settings.ai_neuron_reserve_fraction,
        background_fraction=settings.ai_background_budget_fraction,
        maintenance_fraction=settings.ai_maintenance_budget_fraction,
        embed_neurons_per_char=settings.ai_embed_neurons_per_char,
        chat_neurons_per_char=settings.ai_chat_neurons_per_char,
    )
    embedder = MeteredEmbedder(
        WorkersAIEmbedder(
            env.AI, settings.embedding_model, settings.ai_embed_timeout_seconds
        ),
        budget,
    )
    generator = MeteredGenerator(
        WorkersAIGenerator(
            env.AI, settings.generation_model, settings.ai_generation_timeout_seconds
        ),
        budget,
    )
    vectors = VectorizeStore(env.VECTORIZE)
    lexical = D1LexicalIndex(database)
    listener_messages = D1MessageRepository(database)
    listener_sources = D1SourceRepository(database)
    listener_conversations = D1ConversationRepository(database)
    projection_manifest = D1SearchProjectionRepository(database)
    projector = SearchProjectionService(
        source=D1SearchIndexSource(database),
        embedder=embedder,
        vectors=vectors,
        lexical=lexical,
        manifest=projection_manifest,
        clock=clock,
        budget=budget,
    )
    runtime_smoke = RuntimeSmokeService(
        embedder, generator, vectors, projection_manifest, clock
    )
    pair_candidates = D1MessagePairCandidateRepository(database)
    correction_commits = D1CorrectionCommitStore(database)
    classifier = MessageClassifier(
        embedder=embedder,
        head=load_classifier_head(settings.classifier_model_path),
        confidence_threshold=settings.classifier_confidence_threshold,
        margin_threshold=settings.classifier_margin_threshold,
    )
    return AppContext(
        settings=settings,
        identity=TelegramIdentity(
            admin_user_id=settings.admin_telegram_user_id,
            bot_id=settings.telegram_bot_id,
            bot_username=settings.telegram_bot_username,
            allowed_user_ids=_ids(settings.allowed_telegram_user_ids),
        ),
        clock=clock,
        ingestor=MessageIngestor(
            sources=listener_sources,
            conversations=listener_conversations,
            messages=listener_messages,
            attachments=D1AttachmentRepository(database),
        ),
        classifier=classifier,
        background_indexer=BackgroundIndexer(
            messages=listener_messages,
            conversations=listener_conversations,
            sources=listener_sources,
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
            direct_qa_threshold=settings.direct_qa_threshold,
            synthesis_threshold=settings.synthesis_threshold,
            conversations=listener_conversations,
            sources=listener_sources,
        ),
        reindex=ReindexService(
            source=D1SearchIndexSource(database),
            projector=projector,
            clock=clock,
        ),
        seed=SeedService(
            qa_items=D1QAItemRepository(database),
            qa_versions=D1QAVersionRepository(database),
            sources=D1SourceRepository(database),
            ingestor=MessageIngestor(
                sources=D1SourceRepository(database),
                conversations=D1ConversationRepository(database),
                messages=D1MessageRepository(database),
                attachments=D1AttachmentRepository(database),
            ),
            clock=clock,
        ),
        spaces=SpaceDirectory(
            sources=D1SourceRepository(database),
            conversations=D1ConversationRepository(database),
            spaces=D1SpaceRepository(database),
            bindings=D1ChannelBindingRepository(database),
            clock=clock,
        ),
        review=ReviewService(
            source=D1ReviewSource(database),
            conversations=D1ConversationRepository(database),
        ),
        feedback=FeedbackService(
            answers=answers,
            feedback=D1FeedbackRepository(database),
            qa_items=D1QAItemRepository(database),
            qa_versions=D1QAVersionRepository(database),
            conversations=D1ConversationRepository(database),
            commits=correction_commits,
            clock=clock,
        ),
        interactions=InteractionService(D1TelegramInteractionRepository(database)),
        reviewers=ReviewerManager(
            reviewers=D1ReviewerRepository(database),
            clock=clock,
        ),
        router=ReviewerRouter(
            reviewers=D1ReviewerRepository(database),
            admin_principal_id=f"telegram:{settings.admin_telegram_user_id}",
        ),
        daily_report=DailyReportService(
            source=D1DailyReportSource(database),
            state=D1DailyReportStateRepository(database),
            notifier=notifier,
            budget=budget,
            clock=clock,
            admin_principal_id=f"telegram:{settings.admin_telegram_user_id}",
        ),
        reverter=CorrectionReverter(
            qa_items=D1QAItemRepository(database),
            qa_versions=D1QAVersionRepository(database),
        ),
        budget=budget,
        transport=transport,
        delivery_receipts=D1DeliveryReceiptRepository(database),
        projector=projector,
        runtime_smoke=runtime_smoke,
        pairing=MessagePairingService(
            messages=listener_messages,
            conversations=listener_conversations,
            sources=listener_sources,
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
