# SPDX-License-Identifier: MIT
"""Build an ``AppContext`` from in-memory fakes for HTTP tests."""

import asyncio
from datetime import UTC, datetime

from knowledge_bot.adapters.inbound.telegram import TelegramIdentity
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
from knowledge_bot.domain.entities import ChannelBinding, Space
from knowledge_bot.infrastructure.context import AppContext
from knowledge_bot.infrastructure.settings import Settings
from tests.fakes.ai import (
    FakeEmbedder,
    FakeGenerator,
    FakeLexicalIndex,
    FakeReviewSource,
    FakeSearchIndexSource,
    FakeVectorStore,
    InMemorySearchProjectionRepository,
    linear_head,
)
from tests.fakes.backend import InMemoryBackend
from tests.fakes.support import (
    FrozenClock,
    InMemoryDailyReportSource,
    InMemoryDailyReportStateRepository,
    RecordingNotifier,
    RecordingTransport,
)
from tests.fakes.transactions import InMemoryCorrectionCommitStore

DEFAULT_NOW = datetime(2026, 9, 19, 9, 32, tzinfo=UTC)
WEBHOOK_SECRET = "secret"
ALLOWED_CHAT_ID = "-100"
ALLOWED_CHAT_IDS = "-100,-200"
BOT_ID = "999"
BOT_USERNAME = "bot"
SPACE_A = "sp_" + "1" * 32
SPACE_B = "sp_" + "2" * 32


async def _seed_test_bindings(backend: InMemoryBackend) -> None:
    """Seed the two Telegram groups used by the default test context."""
    for chat_id, space_id in ((ALLOWED_CHAT_ID, SPACE_A), ("-200", SPACE_B)):
        if await backend.spaces.get(space_id) is None:
            await backend.spaces.add(Space(space_id, DEFAULT_NOW, f"Group {chat_id}"))
        if await backend.bindings.get("telegram", chat_id) is None:
            await backend.bindings.add(
                ChannelBinding(
                    "telegram",
                    chat_id,
                    chat_id,
                    space_id,
                    DEFAULT_NOW,
                    f"Group {chat_id}",
                )
            )


def build_test_context(
    *,
    background_listener_enabled: bool = False,
    recap_enabled: bool = False,
    spent_neurons: float = 0.0,
    allowed_user_ids: frozenset[str] = frozenset(),
    admin_report_mode: str = "always",
    reviewer_escalation_timeout_seconds: int = 86_400,
    backend: InMemoryBackend | None = None,
) -> tuple[AppContext, RecordingTransport]:
    """Build a context wired to in-memory fakes."""
    del recap_enabled, admin_report_mode
    backend = backend or InMemoryBackend()
    asyncio.run(_seed_test_bindings(backend))
    if spent_neurons:
        backend.ai_usage.seed(DEFAULT_NOW.strftime("%Y-%m-%d"), spent_neurons)
    transport = RecordingTransport()
    clock = FrozenClock(DEFAULT_NOW)
    embedder = FakeEmbedder()
    vectors = FakeVectorStore()
    lexical = FakeLexicalIndex()
    manifest = InMemorySearchProjectionRepository()
    budget = AiBudget(usage=backend.ai_usage, clock=clock)
    ingestor = MessageIngestor(
        backend.sources, backend.conversations, backend.messages, backend.attachments
    )
    classifier = MessageClassifier(
        embedder=embedder, head=linear_head(len(embedder.vector))
    )
    projector = SearchProjectionService(
        FakeSearchIndexSource(), embedder, vectors, lexical, manifest, clock, budget
    )
    settings = Settings(
        _env_file=None,
        telegram_webhook_secret=WEBHOOK_SECRET,
        telegram_bot_id=BOT_ID,
        telegram_bot_username=BOT_USERNAME,
        admin_telegram_user_id="1",
        internal_admin_key="internal",
        reviewer_escalation_timeout_seconds=reviewer_escalation_timeout_seconds,
        background_listener_enabled=background_listener_enabled,
    )
    identity = TelegramIdentity(
        admin_user_id="1",
        bot_id=BOT_ID,
        bot_username=BOT_USERNAME,
        allowed_user_ids=allowed_user_ids,
    )
    commits = InMemoryCorrectionCommitStore(
        backend.qa_items, backend.qa_versions, backend.qa_evidence, backend.feedback
    )
    context = AppContext(
        settings=settings,
        identity=identity,
        clock=clock,
        ingestor=ingestor,
        classifier=classifier,
        background_indexer=BackgroundIndexer(
            backend.messages,
            backend.conversations,
            backend.sources,
            classifier,
            projector,
            clock,
            0.60,
            0.15,
            budget,
        ),
        answer=AnswerService(
            RetrievalService(embedder, vectors, lexical),
            FakeGenerator(),
            backend.answers,
            clock,
            conversations=backend.conversations,
            sources=backend.sources,
        ),
        reindex=ReindexService(FakeSearchIndexSource(), projector, clock),
        seed=SeedService(
            backend.qa_items, backend.qa_versions, backend.sources, ingestor, clock
        ),
        spaces=SpaceDirectory(
            backend.sources,
            backend.conversations,
            backend.spaces,
            backend.bindings,
            clock,
        ),
        review=ReviewService(FakeReviewSource(), backend.conversations),
        feedback=FeedbackService(
            backend.answers,
            backend.feedback,
            backend.qa_items,
            backend.qa_versions,
            backend.conversations,
            commits,
            clock,
        ),
        interactions=InteractionService(backend.telegram_interactions),
        reviewers=ReviewerManager(backend.reviewers, clock),
        router=ReviewerRouter(backend.reviewers, "telegram:1"),
        daily_report=DailyReportService(
            InMemoryDailyReportSource(),
            InMemoryDailyReportStateRepository(),
            RecordingNotifier(transport),
            budget,
            clock,
            "telegram:1",
        ),
        reverter=CorrectionReverter(backend.qa_items, backend.qa_versions),
        budget=budget,
        transport=transport,
        delivery_receipts=backend.delivery_receipts,
        pairing=MessagePairingService(
            backend.messages,
            backend.conversations,
            backend.sources,
            backend.message_pair_candidates,
            projector,
            clock,
        ),
        projector=projector,
        runtime_smoke=RuntimeSmokeService(
            embedder, FakeGenerator(), vectors, manifest, clock
        ),
    )
    return context, transport
