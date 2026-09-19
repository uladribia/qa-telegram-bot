# SPDX-License-Identifier: MIT
"""Integration tests for the full answer flow (fake embedder/store/generator)."""

import json
from datetime import UTC, datetime

from knowledge_bot.application.answer_question import ABSTENTION_TEXT, AnswerService
from knowledge_bot.application.retrieval import RetrievalService
from knowledge_bot.contracts.messages import NormalizedMessage
from knowledge_bot.domain.enums import AnswerMode, ContentType
from knowledge_bot.ports.generator import GenerationResult
from knowledge_bot.ports.vector_store import VectorRecord
from tests.fakes.ai import FakeEmbedder, FakeGenerator, FakeVectorStore
from tests.fakes.repositories import InMemoryBotAnswerRepository
from tests.fakes.support import FrozenClock, RecordingTransport

NOW = datetime(2026, 9, 19, 9, 32, tzinfo=UTC)


def _message(text: str) -> NormalizedMessage:
    return NormalizedMessage(
        id="m1",
        source_type="telegram",
        conversation_id="-100",
        sender_is_admin=False,
        timestamp=NOW,
        content_type=ContentType.TEXT,
        source_message_id="m1",
        sender_id="hash",
        text=text,
    )


async def _service(
    records: list[VectorRecord],
    result: GenerationResult | None = None,
) -> tuple[
    AnswerService,
    InMemoryBotAnswerRepository,
    RecordingTransport,
    FakeGenerator,
]:
    store = FakeVectorStore()
    await store.upsert(records)
    generator = FakeGenerator(result)
    answers = InMemoryBotAnswerRepository()
    transport = RecordingTransport()
    service = AnswerService(
        retrieval=RetrievalService(
            embedder=FakeEmbedder([1.0, 0.0]),
            vectors=store,
            qa_top_k=5,
            message_top_k=5,
        ),
        generator=generator,
        answers=answers,
        transport=transport,
        clock=FrozenClock(NOW),
    )
    return service, answers, transport, generator


def _qa_record() -> VectorRecord:
    return VectorRecord(
        id="qa1",
        values=[1.0, 0.0],
        metadata={
            "kind": "qa_version",
            "status": "active",
            "text": "Els dimarts.",
            "authority": 90,
            "question": "Quan entrenen?",
        },
    )


def _message_record() -> VectorRecord:
    return VectorRecord(
        id="m9",
        values=[1.0, 0.0],
        metadata={"kind": "message", "text": "els dimarts", "authority": 40},
    )


async def test_direct_qa_answer_is_sent_and_persisted() -> None:
    """A strong Q&A match is sent with sources and stored."""
    service, answers, transport, generator = await _service([_qa_record()])
    record = await service.answer(_message("/ask quan entrenen?"))
    assert record is not None
    assert record.answer_mode is AnswerMode.DIRECT_QA
    assert record.question == "quan entrenen?"
    assert generator.requests == []
    conversation_id, text = transport.messages[0]
    assert conversation_id == "-100"
    assert text.startswith("Els dimarts.")
    assert "Fonts:" in text
    stored = await answers.get("ans:m1")
    assert stored is not None
    assert json.loads(stored.sources_json) == ["qa1"]


async def test_synthesis_uses_generator_and_citations() -> None:
    """Message evidence goes through the generator and validates citations."""
    service, _, transport, generator = await _service(
        [_message_record()],
        GenerationResult(
            status="answered", answer="Sí, els dimarts.", source_ids=["m9"]
        ),
    )
    record = await service.answer(_message("/ask quan entrenen?"))
    assert record is not None
    assert record.answer_mode is AnswerMode.SYNTHESIS
    assert len(generator.requests) == 1
    assert transport.messages[0][1].startswith("Sí, els dimarts.")


async def test_no_knowledge_abstains_and_sends_the_abstention() -> None:
    """With an empty index the bot abstains and says so."""
    service, answers, transport, generator = await _service([])
    record = await service.answer(_message("/ask quan entrenen?"))
    assert record is not None
    assert record.answer_mode is AnswerMode.ABSTENTION
    assert record.answer == ABSTENTION_TEXT
    assert transport.messages[0][1] == ABSTENTION_TEXT
    assert generator.requests == []
    assert await answers.get("ans:m1") is not None


async def test_empty_question_is_not_answered() -> None:
    """A bare /ask with no question produces no answer and no message."""
    service, _, transport, _ = await _service([])
    assert await service.answer(_message("/ask")) is None
    assert transport.messages == []
