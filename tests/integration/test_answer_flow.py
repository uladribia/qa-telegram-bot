# SPDX-License-Identifier: MIT
"""Integration tests for durable answer preparation."""

import json
from datetime import UTC, datetime

from knowledge_bot.application.answer_question import ABSTENTION_TEXT, AnswerService
from knowledge_bot.application.retrieval import RetrievalService
from knowledge_bot.contracts.messages import NormalizedMessage, SourceDescriptor
from knowledge_bot.domain.enums import AnswerMode, ContentType
from knowledge_bot.ports.generator import GenerationOutput
from knowledge_bot.ports.vector_store import VectorRecord
from tests.fakes.ai import FakeEmbedder, FakeGenerator, FakeVectorStore
from tests.fakes.repositories import InMemoryBotAnswerRepository
from tests.fakes.support import FrozenClock

NOW = datetime(2026, 9, 19, 9, 32, tzinfo=UTC)
SPACE_ID = "sp_" + "1" * 32
SCOPE_KEY = "space:" + SPACE_ID


def _message(text: str) -> NormalizedMessage:
    return NormalizedMessage(
        id="m1",
        source=SourceDescriptor(
            id="src:telegram:runtime", kind="telegram", authority=40
        ),
        conversation_id="-100",
        space_id=SPACE_ID,
        sender_is_admin=False,
        timestamp=NOW,
        content_type=ContentType.TEXT,
        source_message_id="m1",
        sender_id="hash",
        text=text,
    )


async def _service(
    records: list[VectorRecord], result: GenerationOutput | None = None
) -> tuple[AnswerService, InMemoryBotAnswerRepository, FakeGenerator]:
    store = FakeVectorStore()
    await store.upsert(records)
    generator = FakeGenerator(result)
    answers = InMemoryBotAnswerRepository()
    service = AnswerService(
        retrieval=RetrievalService(
            embedder=FakeEmbedder([1.0, 0.0]),
            vectors=store,
            qa_top_k=5,
            message_top_k=5,
        ),
        generator=generator,
        answers=answers,
        clock=FrozenClock(NOW),
    )
    return service, answers, generator


def _qa_record() -> VectorRecord:
    return VectorRecord(
        id="qa:web-item",
        values=[1.0, 0.0],
        metadata={
            "kind": "qa",
            "object_id": "web-item",
            "version_id": "qav:web-1",
            "canonical_key": "equipment",
            "status": "active",
            "scope_key": "global",
            "text": "Els dimarts.",
            "authority": 90,
            "question": "Quan entrenen?",
        },
    )


def _message_record() -> VectorRecord:
    return VectorRecord(
        id="msg:m9",
        values=[1.0, 0.0],
        metadata={
            "kind": "message_evidence",
            "scope_key": SCOPE_KEY,
            "text": "els dimarts",
            "authority": 40,
        },
    )


async def test_direct_qa_is_persisted_before_delivery() -> None:
    """Answer preparation stores the rendered response and citations."""
    service, answers, generator = await _service([_qa_record()])
    response = await service.answer_message(_message("/ask quan entrenen?"))
    assert response is not None
    assert response.mode is AnswerMode.DIRECT_QA
    assert response.answer_id == "ans:m1"
    assert response.sources[0].source_id == "qa:web-item"
    stored = await answers.get("ans:m1")
    assert stored is not None
    assert stored.rendered_text == response.rendered_text
    assert json.loads(stored.source_details_json)[0]["source_id"] == "qa:web-item"
    assert generator.requests == []


async def test_answer_message_replay_returns_exact_response() -> None:
    """A repeated message reuses the durable answer without another decision."""
    service, _, generator = await _service([_qa_record()])
    message = _message("/ask quan entrenen?")
    first = await service.answer_message(message)
    second = await service.answer_message(message)
    assert first == second
    assert generator.requests == []


async def test_synthesis_uses_generator_and_citations() -> None:
    """Message evidence goes through the generator and validates citations."""
    service, _, generator = await _service(
        [_message_record()],
        GenerationOutput(
            status="answered", answer="Sí, els dimarts.", source_ids=["msg:m9"]
        ),
    )
    response = await service.answer_message(_message("/ask quan entrenen?"))
    assert response is not None
    assert response.mode is AnswerMode.SYNTHESIS
    assert len(generator.requests) == 1
    assert response.sources[0].source_id == "msg:m9"


async def test_no_knowledge_abstains_and_persists() -> None:
    """With an empty index the bot abstains durably."""
    service, answers, generator = await _service([])
    response = await service.answer_message(_message("/ask quan entrenen?"))
    assert response is not None
    assert response.mode is AnswerMode.ABSTENTION
    assert response.answer == ABSTENTION_TEXT
    assert generator.requests == []
    assert await answers.get("ans:m1") is not None


async def test_empty_question_is_not_answered() -> None:
    """A bare ask with no question produces no answer."""
    service, _, _ = await _service([])
    assert await service.answer_message(_message("/ask")) is None
