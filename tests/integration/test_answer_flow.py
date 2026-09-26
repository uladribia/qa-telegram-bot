# SPDX-License-Identifier: MIT
"""Integration tests for durable answer preparation."""

import json
from datetime import UTC, datetime

from knowledge_bot.application.answer_policy import AnswerPolicy
from knowledge_bot.application.answer_question import ABSTENTION_TEXT, AnswerService
from knowledge_bot.application.retrieval import RetrievalService
from knowledge_bot.contracts.messages import NormalizedMessage, SourceDescriptor
from knowledge_bot.domain.enums import AnswerMode, ContentType
from knowledge_bot.ports.generator import GenerationOutput
from knowledge_bot.ports.vector_store import VectorRecord
from tests.fakes.ai import (
    FakeEmbedder,
    FakeGenerator,
    FakeVectorStore,
)
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


async def test_grounded_answer_is_persisted_before_delivery() -> None:
    """Answer preparation stores the rendered response and citations."""
    service, answers, generator = await _service(
        [_qa_record()],
        GenerationOutput(
            status="answered", answer="Els dimarts.", source_ids=["qa:web-item"]
        ),
    )
    response = await service.answer_message(_message("/ask quan entrenen?"))
    assert response is not None
    assert response.mode is AnswerMode.SYNTHESIS
    assert response.answer_id == "ans:m1"
    assert response.sources[0].source_id == "qa:web-item"
    stored = await answers.get("ans:m1")
    assert stored is not None
    assert stored.rendered_text == response.rendered_text
    assert json.loads(stored.source_details_json)[0]["source_id"] == "qa:web-item"
    assert len(generator.requests) == 1


async def test_answer_message_replay_returns_exact_response() -> None:
    """A repeated message reuses the durable answer without another decision."""
    service, _, generator = await _service([_qa_record()])
    message = _message("/ask quan entrenen?")
    first = await service.answer_message(message)
    second = await service.answer_message(message)
    assert first == second
    assert len(generator.requests) == 1


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


async def _trace(answers: InMemoryBotAnswerRepository) -> dict:
    """Return the stored debug trace of the one persisted answer."""
    stored = await answers.get("ans:m1")
    assert stored is not None
    trace: dict = json.loads(stored.trace_json)
    return trace


async def test_trace_records_why_a_model_refused() -> None:
    """A model refusal is distinguishable from a floor refusal in the trace."""
    service, answers, _ = await _service(
        [_qa_record()], GenerationOutput(status="insufficient")
    )
    await service.answer_message(_message("/ask quan entrenen?"))
    trace = await _trace(answers)
    assert trace["refusal_reason"] == "insufficient"
    assert trace["selected"] == ["qa:web-item"]
    assert trace["candidates"] == {
        "qa": [{"id": "qa:web-item", "similarity": 1.0}],
        "message": [],
    }
    assert trace["generation"]["status"] == "insufficient"
    assert trace["generation"]["source_ids"] == []
    assert trace["generation"]["prompt_chars"] > 0
    assert "cited" not in trace


async def test_trace_records_an_uncited_source_as_unknown() -> None:
    """A cited id outside the evidence is a distinct refusal reason."""
    service, answers, _ = await _service(
        [_qa_record()],
        GenerationOutput(
            status="answered", answer="Divendres.", source_ids=["qa:other"]
        ),
    )
    response = await service.answer_message(_message("/ask quan entrenen?"))
    assert response is not None
    assert response.mode is AnswerMode.ABSTENTION
    trace = await _trace(answers)
    assert trace["refusal_reason"] == "unknown_source_id"
    assert trace["generation"]["source_ids"] == ["qa:other"]


async def test_model_echoing_a_prefixless_source_id_is_answered() -> None:
    """Dropping the kind prefix must not discard a grounded answer.

    This is the exact shape the model returned in production: evidence ids are
    ``qa:<object id>``, and the model cited ``<object id>``.
    """
    service, answers, _ = await _service(
        [_qa_record()],
        GenerationOutput(
            status="answered", answer="Els dimarts.", source_ids=["web-item"]
        ),
    )
    response = await service.answer_message(_message("/ask quan entrenen?"))
    assert response is not None
    assert response.mode is AnswerMode.SYNTHESIS
    assert response.answer == "Els dimarts."
    assert [source.source_id for source in response.sources] == ["qa:web-item"]
    stored = await answers.get("ans:m1")
    assert stored is not None
    assert json.loads(stored.sources_json) == ["qa:web-item"]
    trace = await _trace(answers)
    assert "refusal_reason" not in trace
    assert trace["generation"]["source_ids"] == ["web-item"]
    assert trace["cited"] == ["qa:web-item"]


async def test_both_spellings_of_one_id_cite_the_item_once() -> None:
    """A model citing one id twice, spelled two ways, yields one source."""
    service, answers, _ = await _service(
        [_qa_record()],
        GenerationOutput(
            status="answered",
            answer="Els dimarts.",
            source_ids=["qa:web-item", "web-item"],
        ),
    )
    response = await service.answer_message(_message("/ask quan entrenen?"))
    assert response is not None
    assert response.mode is AnswerMode.SYNTHESIS
    assert len(response.sources) == 1
    stored = await answers.get("ans:m1")
    assert stored is not None
    assert json.loads(stored.sources_json) == ["qa:web-item"]


async def test_trace_records_a_floor_refusal_without_a_model_call() -> None:
    """No evidence above the floor leaves no generation entry at all."""
    service, answers, generator = await _service([], None)
    service = AnswerService(
        retrieval=service.retrieval,
        generator=service.generator,
        answers=service.answers,
        clock=service.clock,
        policy=AnswerPolicy(floor=1.1),
    )
    await service.answer_message(_message("/ask quan entrenen?"))
    trace = await _trace(answers)
    assert generator.requests == []
    assert trace["refusal_reason"] == "no_evidence"
    assert trace["selected"] == []
    assert "generation" not in trace


async def test_trace_records_a_synthesis_without_its_text() -> None:
    """A successful answer is traced by ids, never by its text."""
    service, answers, _ = await _service(
        [_qa_record()],
        GenerationOutput(
            status="answered", answer="Els dimarts.", source_ids=["qa:web-item"]
        ),
    )
    await service.answer_message(_message("/ask quan entrenen?"))
    trace = await _trace(answers)
    assert trace["cited"] == ["qa:web-item"]
    assert "refusal_reason" not in trace
    assert "Els dimarts." not in json.dumps(trace)
