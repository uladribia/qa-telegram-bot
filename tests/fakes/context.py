# SPDX-License-Identifier: MIT
"""Build an ``AppContext`` from in-memory fakes for HTTP tests."""

import asyncio
from datetime import UTC, datetime

from knowledge_bot.adapters.inbound.telegram import TelegramIdentity
from knowledge_bot.application.answer_question import AnswerService
from knowledge_bot.application.budget import AiBudget
from knowledge_bot.application.classifier import MessageClassifier
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
from knowledge_bot.domain.entities import ChannelBinding, Space
from knowledge_bot.infrastructure.composition import AppContext
from knowledge_bot.infrastructure.settings import Settings
from tests.fakes.ai import (
    FakeEmbedder,
    FakeGenerator,
    FakeReviewSource,
    FakeSearchIndexSource,
    FakeVectorStore,
    InMemorySearchProjectionRepository,
)
from tests.fakes.backend import InMemoryBackend
from tests.fakes.support import FrozenClock, RecordingTransport

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
    values = ((ALLOWED_CHAT_ID, SPACE_A), ("-200", SPACE_B))
    for chat_id, space_id in values:
        if await backend.spaces.get(space_id) is None:
            await backend.spaces.add(
                Space(id=space_id, title=f"Group {chat_id}", created_at=DEFAULT_NOW)
            )
        if await backend.bindings.get("telegram", chat_id) is None:
            await backend.bindings.add(
                ChannelBinding(
                    channel="telegram",
                    external_conversation_id=chat_id,
                    conversation_id=chat_id,
                    space_id=space_id,
                    title=f"Group {chat_id}",
                    created_at=DEFAULT_NOW,
                )
            )


def build_test_context(
    *,
    background_listener_enabled: bool = False,
    recap_enabled: bool = False,
    spent_neurons: float = 0.0,
    allowed_user_ids: frozenset[str] = frozenset(),
    admin_report_mode: str = "always",
    backend: InMemoryBackend | None = None,
) -> tuple[AppContext, RecordingTransport]:
    """Build a context wired to in-memory fakes.

    Args:
        background_listener_enabled: Whether unaddressed traffic is ingested.
        recap_enabled: Whether the recap service may send.
        spent_neurons: Estimated AI spend to pre-load for today, to exercise
            the quota guard.
        allowed_user_ids: Extra users who may open a private chat.
        admin_report_mode: How the admin is informed of reviewer resolutions.
        backend: Optional shared state for tests that need to inspect or reuse it.

    Returns:
        The context and the recording transport used by the recap/answer services.
    """
    backend = backend or InMemoryBackend()
    asyncio.run(_seed_test_bindings(backend))
    if spent_neurons:
        backend.ai_usage.seed(DEFAULT_NOW.strftime("%Y-%m-%d"), spent_neurons)
    answers = backend.answers
    feedback_repo = backend.feedback
    transport = RecordingTransport()
    clock = FrozenClock(DEFAULT_NOW)
    embedder = FakeEmbedder()
    vectors = FakeVectorStore()
    budget = AiBudget(usage=backend.ai_usage, clock=clock)
    ingestor = MessageIngestor(
        sources=backend.sources,
        conversations=backend.conversations,
        messages=backend.messages,
        attachments=backend.attachments,
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
        conversations=backend.conversations,
        state=backend.recap_state,
        transport=transport,
        clock=clock,
        admin_user_id="1",
        enabled=recap_enabled,
        interval_hours=24,
        language="ca",
        budget=budget,
        feedback=feedback_repo,
        messages=ingestor.messages,
    )
    reviewer_repo = backend.reviewers
    reviewer_events = backend.reviewer_events
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
        clock=clock,
        ingestor=ingestor,
        classifier=MessageClassifier(embedder=embedder),
        answer=answer,
        recap=recap,
        reindex=ReindexService(
            source=FakeSearchIndexSource(),
            embedder=embedder,
            vectors=vectors,
            manifest=InMemorySearchProjectionRepository(),
            clock=clock,
        ),
        seed=SeedService(
            qa_items=backend.qa_items,
            qa_versions=backend.qa_versions,
            sources=backend.sources,
            ingestor=ingestor,
            clock=clock,
        ),
        spaces=SpaceDirectory(
            sources=backend.sources,
            conversations=backend.conversations,
            spaces=backend.spaces,
            bindings=backend.bindings,
            clock=clock,
        ),
        review=ReviewService(
            source=FakeReviewSource(), conversations=backend.conversations
        ),
        feedback=FeedbackService(
            answers=answers,
            feedback=feedback_repo,
            qa_items=backend.qa_items,
            qa_versions=backend.qa_versions,
            evidence=backend.qa_evidence,
            conversations=backend.conversations,
            clock=clock,
        ),
        feedback_repo=feedback_repo,
        reviewers=ReviewerManager(reviewers=reviewer_repo, clock=clock),
        router=ReviewerRouter(reviewers=reviewer_repo, admin_user_id="1"),
        reviewer_report=ReviewerReportService(
            events=reviewer_events,
            state=backend.report_state,
            transport=transport,
            clock=clock,
            admin_user_id="1",
            mode=admin_report_mode,
        ),
        reverter=CorrectionReverter(
            qa_items=backend.qa_items,
            qa_versions=backend.qa_versions,
        ),
        budget=budget,
        transport=transport,
    )
    return context, transport
