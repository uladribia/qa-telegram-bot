# SPDX-License-Identifier: MIT
"""Tests for the gate that decides whether to speak without being asked."""

from datetime import UTC, datetime

from knowledge_bot.application.answer_question import AnswerService
from knowledge_bot.application.budget import AiBudget
from knowledge_bot.application.proactive import (
    PROACTIVE_ANSWERED,
    PROACTIVE_BUDGET_BLOCKED,
    PROACTIVE_SUPPRESSED,
    ProactiveResponder,
)
from knowledge_bot.application.retrieval import RetrievalService
from knowledge_bot.domain.enums import AiWorkClass, AnswerMode, ContentType
from knowledge_bot.models.common import SourceDescriptor
from knowledge_bot.models.messages import NormalizedMessage
from knowledge_bot.ports.generator import GenerationOutput
from knowledge_bot.ports.vector_store import VectorRecord
from tests.fakes.ai import FakeEmbedder, FakeGenerator, FakeVectorStore
from tests.fakes.repositories import InMemoryBotAnswerRepository
from tests.fakes.support import FrozenClock, InMemoryAiUsageRepository

NOW = datetime(2026, 9, 19, 9, 32, tzinfo=UTC)
SPACE_ID = "sp_" + "1" * 32
ANSWER_ID = "ans:-100:10:proactive:sp_1"


def _message(text: str = "Quan entrenen?") -> NormalizedMessage:
    return NormalizedMessage(
        id="-100:10",
        source=SourceDescriptor(
            id="src:telegram:runtime", kind="telegram", authority=40
        ),
        conversation_id="-100",
        space_id=SPACE_ID,
        content_type=ContentType.TEXT,
        timestamp=NOW,
        text=text,
        sender_is_admin=False,
    )


def _qa_record() -> VectorRecord:
    return VectorRecord(
        id="qa:web-item",
        values=[1.0, 0.0],
        metadata={
            "kind": "qa",
            "object_id": "web-item",
            "version_id": "qav:web-1",
            "canonical_key": "horari",
            "status": "active",
            "scope_key": "global",
            "text": "Els dimarts a les sis.",
            "authority": 90,
            "question": "Quan entrenen?",
        },
    )


async def _responder(
    *,
    record: VectorRecord | None = None,
    result: GenerationOutput | None = None,
    spent: float = 0.0,
) -> tuple[ProactiveResponder, AiBudget]:
    usage = InMemoryAiUsageRepository()
    usage.seed(NOW.strftime("%Y-%m-%d"), spent)
    clock = FrozenClock(NOW)
    budget = AiBudget(usage=usage, clock=clock)
    vectors = FakeVectorStore()
    if record is not None:
        await vectors.upsert([record])
    answer = AnswerService(
        RetrievalService(FakeEmbedder([1.0, 0.0]), vectors),
        FakeGenerator(result),
        InMemoryBotAnswerRepository(),
        clock,
    )
    return ProactiveResponder(answer=answer, budget=budget), budget


async def test_a_synthesis_is_delivered() -> None:
    """A confident question with evidence earns an answer."""
    responder, _ = await _responder(record=_qa_record())
    outcome = await responder.respond(
        _message(), space_id=SPACE_ID, answer_id=ANSWER_ID
    )
    assert outcome.status == PROACTIVE_ANSWERED
    assert outcome.response is not None
    assert outcome.response.answer_id == ANSWER_ID


async def test_an_abstention_is_never_sent_uninvited() -> None:
    """Uninvited silence is the right answer when the model declines."""
    responder, _ = await _responder(
        record=_qa_record(), result=GenerationOutput(status="insufficient")
    )
    outcome = await responder.respond(
        _message(), space_id=SPACE_ID, answer_id=ANSWER_ID
    )
    assert outcome.status == PROACTIVE_SUPPRESSED
    assert outcome.mode is AnswerMode.ABSTENTION
    assert outcome.response is None


async def test_a_provider_failure_is_never_sent_uninvited() -> None:
    """A degraded provider must not turn into a group-wide notice."""
    responder, _ = await _responder(
        record=_qa_record(), result=GenerationOutput(status="insufficient")
    )
    outcome = await responder.respond(
        _message(), space_id=SPACE_ID, answer_id=ANSWER_ID
    )
    assert outcome.status == PROACTIVE_SUPPRESSED
    assert outcome.response is None


async def test_a_message_with_no_question_is_never_answered() -> None:
    """The listener stored it; a message with no text has nothing to ask."""
    responder, _ = await _responder(record=_qa_record())
    outcome = await responder.respond(
        _message("   "), space_id=SPACE_ID, answer_id=ANSWER_ID
    )
    assert outcome.status == PROACTIVE_SUPPRESSED
    assert outcome.mode is None


async def test_the_daily_budget_stops_uninvited_answers_first() -> None:
    """Past the proactive ceiling nothing is spent, however answerable it is."""
    responder, _ = await _responder(record=_qa_record(), spent=4_000.0)
    outcome = await responder.respond(
        _message(), space_id=SPACE_ID, answer_id=ANSWER_ID
    )
    assert outcome.status == PROACTIVE_BUDGET_BLOCKED
    assert outcome.response is None


async def test_the_proactive_ceiling_is_below_the_background_one() -> None:
    """Uninvited answers stop before the indexing that feeds tomorrow."""
    _, budget = await _responder(spent=4_000.0)
    spend = await budget.spend()
    assert spend.proactive_ceiling < spend.background_ceiling
    assert not await budget.work_allowed(AiWorkClass.PROACTIVE)
    assert await budget.work_allowed(AiWorkClass.BACKGROUND)
    assert await budget.work_allowed(AiWorkClass.USER)
