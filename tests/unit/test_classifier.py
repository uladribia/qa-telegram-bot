# SPDX-License-Identifier: MIT
"""Tests for the embedding-prototype message classifier."""

import asyncio

from knowledge_bot.application.classifier import (
    QUESTION,
    IntentScores,
    MessageClassifier,
)
from tests.fakes.ai import FakeEmbedder

PROTOTYPES: dict[str, tuple[str, ...]] = {
    "question": ("qp",),
    "knowledge_update": ("up",),
    "correction": ("cp",),
    "chitchat": ("cc",),
}

Q = [1.0, 0.0]
U = [0.0, 1.0]


def _classifier(texts: dict[str, list[float]]) -> MessageClassifier:
    return MessageClassifier(
        embedder=FakeEmbedder(by_text=texts), prototypes=PROTOTYPES
    )


def test_classify_scores_each_label_by_best_prototype() -> None:
    """A text identical to the question prototype scores 1.0 there."""
    classifier = _classifier({"hola?": Q, "qp": Q, "up": U, "cp": U, "cc": U})
    scores = asyncio.run(classifier.classify("hola?"))
    assert scores.question == 1.0
    assert scores.knowledge_update == 0.0
    assert scores.correction == 0.0
    assert scores.chitchat == 0.0
    assert scores.best() == (QUESTION, 1.0)


def test_prototypes_are_embedded_once_per_process() -> None:
    """The first call batches everything; later calls embed only the text."""
    embedder = FakeEmbedder(
        by_text={"hola?": Q, "adeu!": U, "qp": Q, "up": U, "cp": U, "cc": U}
    )
    classifier = MessageClassifier(
        embedder=embedder,
        prototypes={**PROTOTYPES, "question": ("qp-x",)},
    )
    asyncio.run(classifier.classify("hola?"))
    asyncio.run(classifier.classify("adeu!"))
    assert len(embedder.calls) == 2
    assert len(embedder.calls[0]) == 5
    assert embedder.calls[1] == ["adeu!"]


def test_pure_chitchat_is_discarded() -> None:
    """Strong chitchat with no other signal is not worth keeping."""
    classifier = _classifier({})
    assert not classifier.should_keep(
        IntentScores(question=0.1, knowledge_update=0.2, correction=0.0, chitchat=0.95)
    )


def test_chitchat_with_a_real_signal_is_kept() -> None:
    """A keep signal above the bar saves even a chatty message."""
    classifier = _classifier({})
    assert classifier.should_keep(
        IntentScores(question=0.5, knowledge_update=0.1, correction=0.0, chitchat=0.95)
    )


def test_mild_chitchat_is_always_kept() -> None:
    """Below the discard bar, everything is kept as context."""
    classifier = _classifier({})
    assert classifier.should_keep(
        IntentScores(question=0.1, knowledge_update=0.1, correction=0.0, chitchat=0.5)
    )


def test_pair_matching_bars() -> None:
    """Questions and answers are recognized only above their bars."""
    classifier = _classifier({})
    assert classifier.is_question(IntentScores(question=0.7))
    assert not classifier.is_question(IntentScores(question=0.5))
    assert classifier.is_answer_like(IntentScores(knowledge_update=0.6))
    assert classifier.is_answer_like(IntentScores(correction=0.9))
    assert not classifier.is_answer_like(IntentScores(chitchat=0.9))
