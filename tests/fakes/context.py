# SPDX-License-Identifier: MIT
"""Build an ``AppContext`` from in-memory fakes for HTTP tests."""

from datetime import UTC, datetime

from knowledge_bot.adapters.inbound.telegram import TelegramIdentity
from knowledge_bot.application.answer_question import AnswerService
from knowledge_bot.application.budget import AiBudget
from knowledge_bot.application.classifier import MessageClassifier
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
from knowledge_bot.infrastructure.composition import AppContext
from knowledge_bot.infrastructure.settings import Settings
from tests.fakes.ai import (
    FakeEmbedder,
    FakeGenerator,
    FakeReviewSource,
    FakeSearchIndexSource,
    FakeVectorStore,
)
from tests.fakes.repositories import (
    InMemoryAttachmentRepository,
    InMemoryBotAnswerRepository,
    InMemoryConversationRepository,
    InMemoryFeedbackRepository,
    InMemoryMessageRepository,
    InMemoryQAEvidenceRepository,
    InMemoryQAItemRepository,
    InMemoryQAVersionRepository,
    InMemoryReviewerEventRepository,
    InMemoryReviewerRepository,
    InMemorySourceRepository,
)
from tests.fakes.support import (
    FrozenClock,
    InMemoryAiUsageRepository,
    InMemoryRecapStateRepository,
    InMemoryReportStateRepository,
    RecordingTransport,
)

DEFAULT_NOW = datetime(2026, 9, 19, 9, 32, tzinfo=UTC)
WEBHOOK_SECRET = "secret"
ALLOWED_CHAT_ID = "-100"
ALLOWED_CHAT_IDS = "-100,-200"
BOT_ID = "999"
BOT_USERNAME = "bot"


def build_test_context(
    *,
    background_listener_enabled: bool = False,
    recap_enabled: bool = False,
    spent_neurons: float = 0.0,
    allowed_user_ids: frozenset[str] = frozenset(),
    admin_report_mode: str = "always",
) -> tuple[AppContext, RecordingTransport]:
    """Build a context wired to in-memory fakes.

    Args:
        background_listener_enabled: Whether unaddressed traffic is ingested.
        recap_enabled: Whether the recap service may send.
        spent_neurons: Estimated AI spend to pre-load for today, to exercise
            the quota guard.
        allowed_user_ids: Extra users who may open a private chat.
        admin_report_mode: How the admin is informed of reviewer resolutions.

    Returns:
        The context and the recording transport used by the recap/answer services.
    """
    answers = InMemoryBotAnswerRepository()
    feedback_repo = InMemoryFeedbackRepository()
    transport = RecordingTransport()
    clock = FrozenClock(DEFAULT_NOW)
    embedder = FakeEmbedder()
    vectors = FakeVectorStore()
    ingestor = MessageIngestor(
        sources=InMemorySourceRepository(),
        conversations=InMemoryConversationRepository(),
        messages=InMemoryMessageRepository(),
        attachments=InMemoryAttachmentRepository(),
    )
    answer = AnswerService(
        retrieval=RetrievalService(embedder=embedder, vectors=vectors),
        generator=FakeGenerator(),
        answers=answers,
        transport=transport,
        clock=clock,
    )
    recap = RecapService(
        answers=answers,
        conversations=InMemoryConversationRepository(),
        state=InMemoryRecapStateRepository(),
        transport=transport,
        clock=clock,
        admin_user_id="1",
        enabled=recap_enabled,
        interval_hours=24,
        language="ca",
        budget=AiBudget(usage=_usage_with(spent_neurons), clock=clock),
        feedback=feedback_repo,
        messages=ingestor.messages,
    )
    reviewer_repo = InMemoryReviewerRepository()
    reviewer_events = InMemoryReviewerEventRepository()
    settings = Settings(
        telegram_webhook_secret=WEBHOOK_SECRET,
        telegram_bot_id=BOT_ID,
        telegram_bot_username=BOT_USERNAME,
        allowed_telegram_chat_ids=ALLOWED_CHAT_IDS,
        admin_telegram_user_id="1",
        internal_admin_key="internal",
        background_listener_enabled=background_listener_enabled,
        recap_enabled=recap_enabled,
    )
    identity = TelegramIdentity(
        allowed_chat_ids=frozenset({ALLOWED_CHAT_ID, "-200"}),
        admin_user_id="1",
        bot_id=BOT_ID,
        bot_username=BOT_USERNAME,
        allowed_user_ids=allowed_user_ids,
    )
    context = AppContext(
        settings=settings,
        identity=identity,
        ingestor=ingestor,
        classifier=MessageClassifier(embedder=embedder),
        answer=answer,
        recap=recap,
        reindex=ReindexService(
            source=FakeSearchIndexSource(),
            embedder=embedder,
            vectors=vectors,
        ),
        seed=SeedService(
            qa_items=InMemoryQAItemRepository(),
            qa_versions=InMemoryQAVersionRepository(),
            sources=InMemorySourceRepository(),
            ingestor=ingestor,
            clock=clock,
        ),
        groups=GroupRegistrar(
            sources=InMemorySourceRepository(),
            conversations=InMemoryConversationRepository(),
            clock=clock,
        ),
        review=ReviewService(
            source=FakeReviewSource(), conversations=InMemoryConversationRepository()
        ),
        feedback=FeedbackService(
            answers=answers,
            feedback=feedback_repo,
            qa_items=InMemoryQAItemRepository(),
            qa_versions=InMemoryQAVersionRepository(),
            evidence=InMemoryQAEvidenceRepository(),
            conversations=InMemoryConversationRepository(),
            clock=clock,
        ),
        feedback_repo=feedback_repo,
        reviewers=ReviewerManager(reviewers=reviewer_repo, clock=clock),
        router=ReviewerRouter(reviewers=reviewer_repo, admin_user_id="1"),
        reviewer_report=ReviewerReportService(
            events=reviewer_events,
            state=InMemoryReportStateRepository(),
            transport=transport,
            clock=clock,
            admin_user_id="1",
            mode=admin_report_mode,
        ),
        reverter=CorrectionReverter(
            qa_items=InMemoryQAItemRepository(),
            qa_versions=InMemoryQAVersionRepository(),
        ),
        budget=AiBudget(
            usage=_usage_with(spent_neurons),
            clock=clock,
        ),
        transport=transport,
    )
    return context, transport


def _usage_with(spent_neurons: float) -> InMemoryAiUsageRepository:
    """Return a usage ledger pre-loaded with today's estimated spend."""
    usage = InMemoryAiUsageRepository()
    if spent_neurons:
        usage.seed(DEFAULT_NOW.strftime("%Y-%m-%d"), spent_neurons)
    return usage
