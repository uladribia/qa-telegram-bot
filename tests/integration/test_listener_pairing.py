# SPDX-License-Identifier: MIT
"""Integration coverage for the conservative deterministic listener."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from knowledge_bot.contracts.messages import NormalizedMessage, SourceDescriptor
from knowledge_bot.domain.enums import ClassificationStatus, ContentType, IndexStatus
from tests.fakes.context import build_test_context
from tests.fakes.support import FrozenClock

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
CONFIDENT = {"question": 0.9, "margin": 0.6}


def _message(
    message_id: str, text: str, timestamp: datetime = NOW
) -> NormalizedMessage:
    return NormalizedMessage(
        id=message_id,
        source=SourceDescriptor(id="listener-source", kind="test", authority=40),
        conversation_id="conversation-1",
        sender_is_admin=False,
        timestamp=timestamp,
        content_type=ContentType.TEXT,
        text=text,
    )


def test_temporal_pair_becomes_normal_message_evidence() -> None:
    """A confident answer pairs with the single recent confident question."""
    context, _ = build_test_context()

    async def run() -> None:
        assert isinstance(context.clock, FrozenClock)
        context.clock.advance_to(NOW)
        await context.ingestor.ingest(
            _message("message-question", "Quan entrenem?"),
            intent_label="question",
            intent_score=0.90,
            intent_scores=CONFIDENT,
            classification_status=ClassificationStatus.CLASSIFIED,
            index_status=IndexStatus.NOT_ELIGIBLE,
        )
        await context.ingestor.ingest(
            _message("message-answer", "A les sis."),
            intent_label="knowledge_update",
            intent_score=0.90,
            intent_scores=CONFIDENT,
            classification_status=ClassificationStatus.CLASSIFIED,
            index_status=IndexStatus.NOT_ELIGIBLE,
        )
        assert await context.pairing.on_message("message-question") == 0
        assert await context.pairing.on_message("message-answer") == 1
        matches = await context.answer.retrieval.vectors.query(
            [1.0, 0.0], top_k=5, filters={"kind": "message_evidence"}
        )
        assert len(matches) == 1
        assert matches[0].id == "msg:message-answer"
        assert matches[0].metadata["question"] == "Quan entrenem?"
        assert matches[0].metadata["text"] == "A les sis."

    asyncio.run(run())


def test_temporal_pairing_is_idempotent() -> None:
    """Re-processing the same answer does not duplicate the pair."""
    context, _ = build_test_context()

    async def run() -> None:
        assert isinstance(context.clock, FrozenClock)
        context.clock.advance_to(NOW)
        await context.ingestor.ingest(
            _message("message-question", "Quan entrenem?"),
            intent_label="question",
            intent_score=0.90,
            intent_scores=CONFIDENT,
            classification_status=ClassificationStatus.CLASSIFIED,
        )
        await context.ingestor.ingest(
            _message("message-answer", "A les sis."),
            intent_label="knowledge_update",
            intent_score=0.90,
            intent_scores=CONFIDENT,
            classification_status=ClassificationStatus.CLASSIFIED,
        )
        assert await context.pairing.on_message("message-answer") == 1
        stored = await context.ingestor.get_message("message-answer")
        assert stored is not None
        assert stored.context_question == "Quan entrenem?"
        assert await context.pairing.on_message("message-answer") == 0
        matches = await context.answer.retrieval.vectors.query(
            [1.0, 0.0], top_k=5, filters={"kind": "message_evidence"}
        )
        assert len(matches) == 1

    asyncio.run(run())


def test_two_recent_questions_are_ambiguous_and_not_paired() -> None:
    """With more than one plausible question, the listener refuses to pair."""
    context, _ = build_test_context()

    async def run() -> None:
        assert isinstance(context.clock, FrozenClock)
        context.clock.advance_to(NOW)
        for question_id in ("question-1", "question-2"):
            await context.ingestor.ingest(
                _message(question_id, f"Pregunta {question_id}?"),
                intent_label="question",
                intent_score=0.90,
                intent_scores=CONFIDENT,
                classification_status=ClassificationStatus.CLASSIFIED,
            )
        await context.ingestor.ingest(
            _message("message-answer", "A les sis."),
            intent_label="knowledge_update",
            intent_score=0.90,
            intent_scores=CONFIDENT,
            classification_status=ClassificationStatus.CLASSIFIED,
        )
        assert await context.pairing.on_message("message-answer") == 0
        stored = await context.ingestor.get_message("message-answer")
        assert stored is not None
        assert stored.context_question is None

    asyncio.run(run())


def test_ambiguous_question_is_never_a_pending_candidate() -> None:
    """A question without margin does not attract a later pairing."""
    context, _ = build_test_context()

    async def run() -> None:
        assert isinstance(context.clock, FrozenClock)
        context.clock.advance_to(NOW)
        await context.ingestor.ingest(
            _message("message-question", "Diumenge?"),
            intent_label="question",
            intent_score=0.55,
            intent_scores={"question": 0.55, "margin": 0.05},
            classification_status=ClassificationStatus.CLASSIFIED,
        )
        await context.ingestor.ingest(
            _message("message-answer", "A les sis."),
            intent_label="knowledge_update",
            intent_score=0.90,
            intent_scores=CONFIDENT,
            classification_status=ClassificationStatus.CLASSIFIED,
        )
        assert await context.pairing.on_message("message-answer") == 0
        stored = await context.ingestor.get_message("message-answer")
        assert stored is not None
        assert stored.context_question is None

    asyncio.run(run())


def test_old_questions_leave_the_pairing_window() -> None:
    """A question older than the window never pairs."""
    context, _ = build_test_context()

    async def run() -> None:
        assert isinstance(context.clock, FrozenClock)
        stale = NOW - timedelta(minutes=10)
        context.clock.advance_to(stale)
        await context.ingestor.ingest(
            _message("message-question", "Quan entrenem?", stale),
            intent_label="question",
            intent_score=0.90,
            intent_scores=CONFIDENT,
            classification_status=ClassificationStatus.CLASSIFIED,
        )
        context.clock.advance_to(NOW)
        await context.ingestor.ingest(
            _message("message-answer", "A les sis."),
            intent_label="knowledge_update",
            intent_score=0.90,
            intent_scores=CONFIDENT,
            classification_status=ClassificationStatus.CLASSIFIED,
        )
        assert await context.pairing.on_message("message-answer") == 0

    asyncio.run(run())


def test_chitchat_and_unclassified_messages_are_never_paired() -> None:
    """Only confident factual updates may pair; the rest stay stored."""
    context, _ = build_test_context()

    async def run() -> None:
        assert isinstance(context.clock, FrozenClock)
        context.clock.advance_to(NOW)
        await context.ingestor.ingest(
            _message("message-question", "Quan entrenem?"),
            intent_label="question",
            intent_score=0.90,
            intent_scores=CONFIDENT,
            classification_status=ClassificationStatus.CLASSIFIED,
        )
        await context.ingestor.ingest(
            _message("message-chitchat", "gràcies!"),
            classification_status=ClassificationStatus.PREFILTER_CHITCHAT,
        )
        assert await context.pairing.on_message("message-chitchat") == 0
        assert await context.pairing.on_message("missing-id") == 0

    asyncio.run(run())
