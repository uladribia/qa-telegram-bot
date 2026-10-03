# SPDX-License-Identifier: MIT
"""The bot modes decide who pays for a decision, and how often.

The decision model replaces the listener's classification only. Which messages
reach it, and how many times, is mode behaviour: a disabled conversation never
decides, an addressed message answers without deciding anything, and a
proactive conversation must not pay for a second look at the same message.
These tests pin that matrix on call counts, not on prose.
"""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from knowledge_bot.adapters.telegram.routes import TELEGRAM_WEBHOOK_PATH
from knowledge_bot.api.app import create_app
from knowledge_bot.application.assessment import BaselineAssessmentModel
from knowledge_bot.application.classifier import (
    Classification,
    MessageClassifier,
)
from knowledge_bot.application.listener import ListenerIngestor
from knowledge_bot.domain.enums import BotMode
from knowledge_bot.infrastructure.context import AppContext
from knowledge_bot.models.assessment import MessageAssessment, RetroevalCandidate
from tests.fakes.ai import FakeEmbedder, FakeGenerator, linear_head
from tests.fakes.context import build_test_context

pytestmark = pytest.mark.integration
SECRET_HEADER = {"X-Telegram-Bot-Api-Secret-Token": "secret"}

QUESTION_VECTOR = [1.0, 0.0, 0.0, 0.0]

VECTORS = {
    "Quan entrenen?": QUESTION_VECTOR,
    "Els dimarts a les sis.": [0.0, 1.0, 0.0, 0.0],
}


class CountingAssessment:
    """Wrap an assessment model and count how often it is asked to decide."""

    def __init__(self, inner: BaselineAssessmentModel) -> None:
        """Wrap the backend whose calls should be counted."""
        self.inner = inner
        self.calls: list[str] = []

    @property
    def confidence_threshold(self) -> float:
        """The wrapped model's confidence policy."""
        return self.inner.confidence_threshold

    @property
    def margin_threshold(self) -> float:
        """The wrapped model's margin policy."""
        return self.inner.margin_threshold

    async def assess(
        self, text: str, *, candidates: tuple[RetroevalCandidate, ...] = ()
    ) -> MessageAssessment:
        """Count the call, then let the wrapped model decide."""
        self.calls.append(text)
        return await self.inner.assess(text, candidates=candidates)

    def is_question(self, classification: Classification) -> bool:
        """The wrapped model's question rule."""
        return self.inner.is_question(classification)

    def is_answer_like(self, classification: Classification) -> bool:
        """The wrapped model's answer rule."""
        return self.inner.is_answer_like(classification)


def _mode_context(
    mode: BotMode, *, dm_mode: BotMode | None = None
) -> tuple[AppContext, CountingAssessment, FakeGenerator]:
    """Build a context whose decision model counts its own calls."""
    embedder = FakeEmbedder(vector=QUESTION_VECTOR, by_text=VECTORS)
    generator = FakeGenerator()
    context, _ = build_test_context(
        group_bot_mode=mode,
        dm_bot_mode=dm_mode or mode,
        embedder=embedder,
        generator=generator,
    )
    classifier = MessageClassifier(embedder=embedder, head=linear_head(4))
    counting = CountingAssessment(BaselineAssessmentModel(classifier))
    pairing = replace(context.pairing, assessment=counting)
    listener = ListenerIngestor(
        ingestor=context.ingestor,
        assessment=counting,
        budget=context.budget,
        pairing=pairing,
        background_indexer=context.background_indexer,
    )
    return (
        replace(context, assessment=counting, pairing=pairing, listener=listener),
        counting,
        generator,
    )


def _post(context: AppContext, text: str, *, chat_type: str = "supergroup") -> Response:
    """Send one group update through the webhook."""
    message = {
        "message_id": 10,
        "date": 1789000000,
        "chat": {"id": -100, "type": chat_type},
        "from": {"id": 111, "is_bot": False},
        "text": text,
    }
    with TestClient(create_app(lambda request: context)) as client:
        return client.post(
            TELEGRAM_WEBHOOK_PATH,
            json={"update_id": 1, "message": message},
            headers=SECRET_HEADER,
        )


def test_a_disabled_conversation_never_decides() -> None:
    """``off`` spends nothing on traffic nobody addressed to the bot."""
    context, counting, generator = _mode_context(BotMode.OFF)

    _post(context, "Quan entrenen?")

    assert counting.calls == []
    assert generator.requests == []


def test_a_silent_conversation_decides_once_and_never_answers() -> None:
    """``silent`` keeps listening without ever spending on a generation."""
    context, counting, generator = _mode_context(BotMode.SILENT)

    _post(context, "Quan entrenen?")

    assert len(counting.calls) == 1
    assert generator.requests == []


def test_an_active_conversation_decides_unaddressed_traffic_once() -> None:
    """``active`` decides an unaddressed message exactly once."""
    context, counting, _ = _mode_context(BotMode.ACTIVE)

    _post(context, "Quan entrenen?")

    assert len(counting.calls) == 1


def test_an_addressed_message_never_reaches_the_decision_model() -> None:
    """A direct answer path has no listener decision to make."""
    context, counting, _ = _mode_context(BotMode.ACTIVE)

    _post(context, "/ask quan entrenen?")

    assert counting.calls == []


def test_a_proactive_conversation_decides_a_message_once() -> None:
    """Proactive answering reuses the decision, it never repeats it."""
    context, counting, _ = _mode_context(BotMode.PROACTIVE)

    _post(context, "Quan entrenen?")

    assert len(counting.calls) == 1


def test_a_private_message_never_reaches_the_decision_model() -> None:
    """A DM always addresses the bot, so the listener never sees it."""
    context, counting, _ = _mode_context(BotMode.ACTIVE)

    _post(context, "Quan entrenen?", chat_type="private")

    assert counting.calls == []
