# SPDX-License-Identifier: MIT
"""Compose the application context from Cloudflare Worker bindings."""

from dataclasses import dataclass
from typing import Protocol

from knowledge_bot.adapters.inbound.telegram import TelegramIdentity
from knowledge_bot.adapters.outbound.telegram import TelegramTransport
from knowledge_bot.application.answer_question import AnswerService
from knowledge_bot.application.budget import AiBudget
from knowledge_bot.application.feedback import FeedbackService
from knowledge_bot.application.groups import GroupRegistrar
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
    D1ConversationRepository,
    D1Database,
    D1FeedbackRepository,
    D1MessageRepository,
    D1QAEvidenceRepository,
    D1QAItemRepository,
    D1QAVersionRepository,
    D1RecapStateRepository,
    D1ReportStateRepository,
    D1ReviewerEventRepository,
    D1ReviewerRepository,
    D1ReviewSource,
    D1SearchIndexSource,
    D1SourceRepository,
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
from knowledge_bot.infrastructure.metering import MeteredEmbedder, MeteredGenerator
from knowledge_bot.infrastructure.settings import Settings
from knowledge_bot.ports.repositories import FeedbackRepository
from knowledge_bot.ports.transport import MessageTransport


class WorkerEnv(Protocol):
    """The Cloudflare ``env`` object: bindings plus optional string variables."""

    DB: D1Database
    AI: AiRunner
    VECTORIZE: VectorizeIndex


@dataclass(frozen=True, slots=True)
class AppContext:
    """Everything the HTTP layer needs, wired once per isolate."""

    settings: Settings
    identity: TelegramIdentity
    ingestor: MessageIngestor
    answer: AnswerService
    recap: RecapService
    reindex: ReindexService
    seed: SeedService
    groups: GroupRegistrar
    review: ReviewService
    feedback: FeedbackService
    feedback_repo: FeedbackRepository
    reviewers: ReviewerManager
    router: ReviewerRouter
    reviewer_report: ReviewerReportService
    reverter: CorrectionReverter
    budget: AiBudget
    transport: MessageTransport


def _text(env: WorkerEnv, name: str, default: str = "") -> str:
    value = getattr(env, name, None)
    return default if value is None else str(value)


def _ids(value: str) -> frozenset[str]:
    """Parse a comma-separated id list into a set of stripped ids."""
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def _flag(env: WorkerEnv, name: str, default: bool = False) -> bool:
    value = getattr(env, name, None)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int(env: WorkerEnv, name: str, default: int) -> int:
    try:
        return int(_text(env, name, str(default)))
    except ValueError:
        return default


def _float(env: WorkerEnv, name: str, default: float) -> float:
    try:
        return float(_text(env, name, str(default)))
    except ValueError:
        return default


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
        allowed_telegram_chat_ids=_text(env, "ALLOWED_TELEGRAM_CHAT_IDS"),
        allowed_telegram_user_ids=_text(env, "ALLOWED_TELEGRAM_USER_IDS"),
        admin_telegram_user_id=_text(env, "ADMIN_TELEGRAM_USER_ID"),
        telegram_bot_id=_text(env, "TELEGRAM_BOT_ID"),
        telegram_bot_username=_text(env, "TELEGRAM_BOT_USERNAME"),
        internal_admin_key=_text(env, "INTERNAL_ADMIN_KEY"),
        recap_enabled=_flag(env, "RECAP_ENABLED", True),
        recap_interval_hours=_int(env, "RECAP_INTERVAL_HOURS", 24),
        recap_language=_text(env, "RECAP_LANGUAGE", "ca") or "ca",
        admin_report_mode=_text(env, "ADMIN_REPORT_MODE", "always") or "always",
        admin_report_interval_min=_int(env, "ADMIN_REPORT_INTERVAL_MIN", 60),
        background_listener_enabled=_flag(env, "BACKGROUND_LISTENER_ENABLED", False),
        direct_qa_threshold=_float(env, "DIRECT_QA_THRESHOLD", 0.7),
        synthesis_threshold=_float(env, "SYNTHESIS_THRESHOLD", 0.3),
        qa_top_k=_int(env, "QA_TOP_K", 5),
        message_top_k=_int(env, "MESSAGE_TOP_K", 8),
        ai_daily_neuron_budget=_float(env, "AI_DAILY_NEURON_BUDGET", 10_000.0),
        ai_neuron_reserve_fraction=_float(env, "AI_NEURON_RESERVE_FRACTION", 0.25),
        ai_embed_neurons_per_char=_float(env, "AI_EMBED_NEURONS_PER_CHAR", 0.015),
        ai_chat_neurons_per_char=_float(env, "AI_CHAT_NEURONS_PER_CHAR", 0.020),
    )
    database = env.DB
    answers = D1BotAnswerRepository(database)
    transport = TelegramTransport(WorkersHttpClient(), settings.telegram_bot_token)
    clock = SystemClock()
    budget = AiBudget(
        usage=D1AiUsageRepository(database),
        clock=clock,
        daily_neurons=settings.ai_daily_neuron_budget,
        reserve_fraction=settings.ai_neuron_reserve_fraction,
        embed_neurons_per_char=settings.ai_embed_neurons_per_char,
        chat_neurons_per_char=settings.ai_chat_neurons_per_char,
    )
    embedder = MeteredEmbedder(
        WorkersAIEmbedder(env.AI, settings.embedding_model), budget
    )
    generator = MeteredGenerator(
        WorkersAIGenerator(env.AI, settings.generation_model), budget
    )
    vectors = VectorizeStore(env.VECTORIZE)
    return AppContext(
        settings=settings,
        identity=TelegramIdentity(
            allowed_chat_ids=frozenset(settings.allowed_chat_ids),
            admin_user_id=settings.admin_telegram_user_id,
            bot_id=settings.telegram_bot_id,
            bot_username=settings.telegram_bot_username,
            allowed_user_ids=_ids(settings.allowed_telegram_user_ids),
        ),
        ingestor=MessageIngestor(
            sources=D1SourceRepository(database),
            conversations=D1ConversationRepository(database),
            messages=D1MessageRepository(database),
            attachments=D1AttachmentRepository(database),
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
            transport=transport,
            clock=clock,
            direct_qa_threshold=settings.direct_qa_threshold,
            synthesis_threshold=settings.synthesis_threshold,
        ),
        recap=RecapService(
            answers=answers,
            conversations=D1ConversationRepository(database),
            state=D1RecapStateRepository(database),
            transport=transport,
            clock=clock,
            admin_user_id=settings.admin_telegram_user_id or None,
            enabled=settings.recap_enabled,
            interval_hours=settings.recap_interval_hours,
            language=settings.recap_language,
        ),
        reindex=ReindexService(
            source=D1SearchIndexSource(database),
            embedder=embedder,
            vectors=vectors,
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
        groups=GroupRegistrar(
            sources=D1SourceRepository(database),
            conversations=D1ConversationRepository(database),
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
            evidence=D1QAEvidenceRepository(database),
            conversations=D1ConversationRepository(database),
            clock=clock,
        ),
        feedback_repo=D1FeedbackRepository(database),
        reviewers=ReviewerManager(
            reviewers=D1ReviewerRepository(database),
            clock=clock,
        ),
        router=ReviewerRouter(
            reviewers=D1ReviewerRepository(database),
            admin_user_id=settings.admin_telegram_user_id,
        ),
        reviewer_report=ReviewerReportService(
            events=D1ReviewerEventRepository(database),
            state=D1ReportStateRepository(database),
            transport=transport,
            clock=clock,
            admin_user_id=settings.admin_telegram_user_id,
            mode=settings.admin_report_mode,
            interval_min=settings.admin_report_interval_min,
            budget=budget,
        ),
        reverter=CorrectionReverter(
            qa_items=D1QAItemRepository(database),
            qa_versions=D1QAVersionRepository(database),
        ),
        budget=budget,
        transport=transport,
    )
