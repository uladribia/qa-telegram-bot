# SPDX-License-Identifier: MIT
"""Integration coverage for temporal listener pairing."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from knowledge_bot.contracts.messages import NormalizedMessage, SourceDescriptor
from knowledge_bot.domain.enums import ClassificationStatus, ContentType, IndexStatus
from knowledge_bot.ports.pairing import PairCandidate, PairingOutput
from tests.fakes.context import build_test_context
from tests.fakes.support import FrozenClock

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)


def _message(message_id: str, text: str) -> NormalizedMessage:
    return NormalizedMessage(
        id=message_id,
        source=SourceDescriptor(id="listener-source", kind="test", authority=40),
        conversation_id="conversation-1",
        sender_is_admin=False,
        timestamp=NOW,
        content_type=ContentType.TEXT,
        text=text,
    )


def test_temporal_pair_becomes_normal_message_evidence() -> None:
    """An accepted temporal pair is projected as stable message evidence."""
    context, _ = build_test_context(
        pairing_output=PairingOutput(
            pairs=[
                PairCandidate(
                    question_id="message-question",
                    answer_id="message-answer",
                    confidence=0.92,
                )
            ]
        )
    )

    async def run() -> None:
        assert isinstance(context.clock, FrozenClock)
        context.clock.advance_to(NOW)
        await context.ingestor.ingest(
            _message("message-question", "Quan entrenem?"),
            classification_status=ClassificationStatus.CLASSIFIED,
            index_status=IndexStatus.NOT_ELIGIBLE,
        )
        await context.ingestor.ingest(
            _message("message-answer", "A les sis."),
            classification_status=ClassificationStatus.CLASSIFIED,
            index_status=IndexStatus.NOT_ELIGIBLE,
        )
        assert await context.pairing.on_message("message-question") == 0
        assert await context.pairing.on_message("message-answer") == 0
        assert await context.pairing.flush_due(NOW + timedelta(minutes=3)) == 1
        matches = await context.answer.retrieval.vectors.query(
            [1.0, 0.0], top_k=5, filters={"kind": "message_evidence"}
        )
        assert len(matches) == 1
        assert matches[0].id == "msg:message-answer"
        assert matches[0].metadata["question"] == "Quan entrenem?"
        assert matches[0].metadata["text"] == "A les sis."

    asyncio.run(run())
