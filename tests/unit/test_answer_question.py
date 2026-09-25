# SPDX-License-Identifier: MIT
"""Tests for the evidence gate and question cleaning."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from knowledge_bot.application.answer_question import AnswerService, clean_question
from knowledge_bot.application.retrieval import (
    Evidence,
    RetrievalService,
    RetrievedEvidence,
)
from knowledge_bot.contracts.messages import NormalizedMessage, SourceDescriptor
from knowledge_bot.domain.enums import AnswerMode
from knowledge_bot.ports.generator import GenerationOutput
from tests.fakes.ai import (
    FakeEmbedder,
    FakeGenerator,
    FakeLexicalIndex,
    FakeVectorStore,
)
from tests.fakes.repositories import InMemoryBotAnswerRepository
from tests.fakes.support import FrozenClock

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _service(
    result: GenerationOutput | None = None,
) -> tuple[AnswerService, FakeGenerator]:
    generator = FakeGenerator(result)
    return AnswerService(
        retrieval=RetrievalService(
            embedder=FakeEmbedder(),
            vectors=FakeVectorStore(),
            lexical=FakeLexicalIndex(),
        ),
        generator=generator,
        answers=InMemoryBotAnswerRepository(),
        clock=FrozenClock(NOW),
    ), generator


def _qa(similarity: float, authority: int = 90) -> Evidence:
    return Evidence(
        "qa1",
        "Q&A",
        "Els dimarts.",
        authority,
        similarity,
        "Quan entrenen?",
        "qa1",
        "qav1",
    )


def _message_evidence(similarity: float) -> Evidence:
    return Evidence("m1", "Grup", "context", 40, similarity)


def test_clean_question_strips_command_and_mentions() -> None:
    """The ask command and mentions are removed."""
    assert clean_question("/ask quan entrenen?") == "quan entrenen?"
    assert clean_question("/ask@bot hola") == "hola"
    assert clean_question("@bot quan?") == "quan?"
    assert clean_question("  hola  ") == "hola"
    assert clean_question("/ask") == ""


async def test_evidence_is_answered_by_the_generator() -> None:
    """A usable Q&A candidate reaches the model, which produces the answer."""
    service, generator = _service()
    outcome = await service.decide("pregunta", RetrievedEvidence(qa=[_qa(0.9)]))
    assert outcome.mode is AnswerMode.SYNTHESIS
    assert outcome.source_ids == ["qa1"]
    assert len(generator.requests) == 1


async def test_evidence_above_the_floor_is_sent_to_the_model() -> None:
    """Every candidate that clears the floor is offered to the generator."""
    service, generator = _service()
    second = Evidence(
        "qa2", "Q&A", "Els dimearts.", 90, 0.85, question="Quan?", qa_version_id="qav2"
    )
    outcome = await service.decide("pregunta", RetrievedEvidence(qa=[_qa(0.9), second]))
    assert outcome.mode is AnswerMode.SYNTHESIS
    assert [item.source_id for item in generator.requests[0].evidence] == [
        "qa1",
        "qa2",
    ]


async def test_evidence_below_the_floor_abstains() -> None:
    """Nothing clears the floor, so the model is never called."""
    service, generator = _service()
    outcome = await service.decide("pregunta", RetrievedEvidence(qa=[_qa(0.4)]))
    assert outcome.mode is AnswerMode.ABSTENTION
    assert generator.requests == []


async def test_model_can_still_decline_with_evidence() -> None:
    """Usable evidence is offered, but the model may return insufficient."""
    service, generator = _service(
        GenerationOutput(status="insufficient", source_ids=[])
    )
    outcome = await service.decide("pregunta", RetrievedEvidence(qa=[_qa(0.9)]))
    assert outcome.mode is AnswerMode.ABSTENTION
    assert len(generator.requests) == 1


async def test_no_evidence_abstains_without_generator() -> None:
    """No evidence means no model call."""
    service, generator = _service()
    outcome = await service.decide("pregunta", RetrievedEvidence())
    assert outcome.mode is AnswerMode.ABSTENTION
    assert generator.requests == []


async def test_synthesis_caps_qa_and_message_evidence() -> None:
    """The generator receives at most five Q&A and three message records."""
    qa = [
        Evidence(f"qa{index}", "Q&A", "text", 90, 0.9, question="Quan?")
        for index in range(7)
    ]
    messages = [_message_evidence(0.75) for _ in range(4)]
    service, generator = _service(
        GenerationOutput(status="answered", answer="resposta", source_ids=["qa1", "m1"])
    )
    outcome = await service.decide(
        "pregunta", RetrievedEvidence(qa=qa, messages=messages)
    )
    assert outcome.mode is AnswerMode.SYNTHESIS
    assert len(generator.requests[0].evidence) == 8


def test_generation_output_rejects_invalid_citation_shapes() -> None:
    """Generator output cannot cite invalid source shapes."""
    with pytest.raises(ValidationError):
        GenerationOutput(
            status="answered", answer="resposta", source_ids=["qa1", "qa1"]
        )
    with pytest.raises(ValidationError):
        GenerationOutput(status="answered", answer="", source_ids=["qa1"])
    with pytest.raises(ValidationError):
        GenerationOutput(status="insufficient", source_ids=["qa1"])


async def test_unknown_source_id_abstains() -> None:
    """A citation outside supplied evidence is rejected."""
    service, _ = _service(
        GenerationOutput(status="answered", answer="resposta", source_ids=["ghost"])
    )
    outcome = await service.decide(
        "pregunta", RetrievedEvidence(messages=[_message_evidence(0.6)])
    )
    assert outcome.mode is AnswerMode.ABSTENTION


async def test_generator_insufficient_abstains() -> None:
    """The model can decline to answer."""
    service, _ = _service(GenerationOutput(status="insufficient"))
    outcome = await service.decide(
        "pregunta", RetrievedEvidence(messages=[_message_evidence(0.6)])
    )
    assert outcome.mode is AnswerMode.ABSTENTION


async def test_model_failure_degrades_without_losing_the_question() -> None:
    """A model failure becomes unavailable and remains durable."""
    service, _ = _service()
    embedder = FakeEmbedder()
    embedder.fail = True
    service = AnswerService(
        RetrievalService(embedder, FakeVectorStore(), FakeLexicalIndex()),
        service.generator,
        service.answers,
        FrozenClock(NOW),
    )
    message = NormalizedMessage(
        id="m1",
        source=SourceDescriptor(
            id="src:telegram:runtime", kind="telegram", authority=40
        ),
        conversation_id="c1",
        content_type="text",
        timestamp=NOW,
        text="on entrenen?",
        sender_is_admin=False,
    )
    response = await service.answer_message(message)
    assert response is not None
    assert response.mode is AnswerMode.UNAVAILABLE
    assert response.answer_id == "ans:m1"
