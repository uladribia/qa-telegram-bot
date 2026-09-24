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
from tests.fakes.ai import FakeEmbedder, FakeGenerator, FakeVectorStore
from tests.fakes.repositories import InMemoryBotAnswerRepository
from tests.fakes.support import FrozenClock

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _service(
    result: GenerationOutput | None = None,
) -> tuple[AnswerService, FakeGenerator]:
    generator = FakeGenerator(result)
    return AnswerService(
        retrieval=RetrievalService(embedder=FakeEmbedder(), vectors=FakeVectorStore()),
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


async def test_strong_qa_is_answered_directly_without_generator() -> None:
    """A strong active Q&A match bypasses the model."""
    service, generator = _service()
    outcome = await service.decide("pregunta", RetrievedEvidence(qa=[_qa(0.9)]))
    assert outcome.mode is AnswerMode.DIRECT_QA
    assert outcome.source_ids == ["qa1"]
    assert outcome.qa_version_id == "qav1"
    assert generator.requests == []


async def test_direct_qa_authority_breaks_equal_similarity_ties() -> None:
    """Equal similarity prefers the higher-authority Q&A item."""
    service, _ = _service()
    low = _qa(0.8, 90)
    high = Evidence(
        "qa2",
        "Q&A",
        "Authoritatiu",
        100,
        0.8,
        question="Quan?",
        qa_item_id="qa2",
        qa_version_id="qav2",
    )
    outcome = await service.decide("pregunta", RetrievedEvidence(qa=[low, high]))
    assert outcome.source_ids == ["qa2"]


async def test_weak_qa_falls_through_to_synthesis() -> None:
    """A weak Q&A match does not bypass the model."""
    service, generator = _service(
        GenerationOutput(status="answered", answer="sintetitzat", source_ids=["qa1"])
    )
    outcome = await service.decide("pregunta", RetrievedEvidence(qa=[_qa(0.4)]))
    assert outcome.mode is AnswerMode.SYNTHESIS
    assert len(generator.requests) == 1


async def test_no_evidence_abstains_without_generator() -> None:
    """No evidence means no model call."""
    service, generator = _service()
    outcome = await service.decide("pregunta", RetrievedEvidence())
    assert outcome.mode is AnswerMode.ABSTENTION
    assert generator.requests == []


async def test_synthesis_caps_qa_and_message_evidence() -> None:
    """The generator receives at most five evidence records."""
    qa = [_qa(0.4) for _ in range(3)]
    messages = [_message_evidence(0.5) for _ in range(4)]
    service, generator = _service(
        GenerationOutput(status="answered", answer="resposta", source_ids=["qa1", "m1"])
    )
    outcome = await service.decide(
        "pregunta", RetrievedEvidence(qa=qa, messages=messages)
    )
    assert outcome.mode is AnswerMode.SYNTHESIS
    assert len(generator.requests[0].evidence) == 5


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
        RetrievalService(embedder, FakeVectorStore()),
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
