# SPDX-License-Identifier: MIT
"""Tests for the linear-head message classifier."""

import asyncio

from knowledge_bot.application.classifier import (
    QUESTION,
    IntentScores,
    MessageClassifier,
    parse_margin,
)
from knowledge_bot.domain.enums import IntentLabel
from tests.fakes.ai import FakeEmbedder, linear_head

Q = [1.0, 0.0, 0.0, 0.0]
U = [0.0, 1.0, 0.0, 0.0]
C = [0.0, 0.0, 1.0, 0.0]
CC = [0.0, 0.0, 0.0, 1.0]
AMBIGUOUS = [0.5, 0.5, 0.0, 0.0]


def _classifier(texts: dict[str, list[float]]) -> MessageClassifier:
    return MessageClassifier(embedder=FakeEmbedder(by_text=texts), head=linear_head())


def test_classify_softmaxes_the_linear_head() -> None:
    """An exact label vector produces a confident softmax for that label."""
    classifier = _classifier({"hola?": Q})
    classification = asyncio.run(classifier.classify("hola?"))
    assert classification.embedding == (1.0, 0.0, 0.0, 0.0)
    assert classification.best_label is QUESTION
    assert classification.best_score > 0.60
    assert classification.margin > 0.15
    assert classification.scores.question == classification.best_score


def test_prefilter_chitchat_skips_embedding() -> None:
    """Exact acknowledgements and emoji-only texts need no AI call."""
    embedder = FakeEmbedder()
    classifier = MessageClassifier(embedder=embedder, head=linear_head())
    for text in ("gràcies", "ok", "😂😂", "9"):
        classification = asyncio.run(classifier.classify(text))
        assert classification.best_label is IntentLabel.CHITCHAT
        assert classification.embedding == ()
    assert embedder.calls == []


def test_each_label_can_win_confidently() -> None:
    """Basis vectors map one-to-one onto the four labels."""
    classifier = _classifier({"hola?": Q, "avís": U, "correcció!": C, "jeje": CC})
    for text, label in (
        ("hola?", QUESTION),
        ("avís", IntentLabel.KNOWLEDGE_UPDATE),
        ("correcció!", IntentLabel.CORRECTION),
        ("jeje", IntentLabel.CHITCHAT),
    ):
        classification = asyncio.run(classifier.classify(text))
        assert classification.best_label is label
        assert classification.confident


def test_low_margin_is_ambiguous() -> None:
    """A tie between the top two labels does not clear the policy."""
    classification = asyncio.run(_classifier({"mitjà": AMBIGUOUS}).classify("mitjà"))
    assert classification.best_score < 0.60 or classification.margin < 0.15
    assert not classification.confident


def test_is_question_and_is_answer_like_require_confidence() -> None:
    """Ambiguous outputs never become pending questions or evidence."""
    classifier = _classifier({"hola?": Q, "mitjà": AMBIGUOUS})
    assert classifier.is_question(asyncio.run(classifier.classify("hola?")))
    assert not classifier.is_question(asyncio.run(classifier.classify("mitjà")))
    answer_classifier = _classifier({"avís": U, "mitjà": AMBIGUOUS})
    assert answer_classifier.is_answer_like(
        asyncio.run(answer_classifier.classify("avís"))
    )
    assert not answer_classifier.is_answer_like(
        asyncio.run(answer_classifier.classify("mitjà"))
    )


def test_head_rejects_mismatched_dimensions() -> None:
    """A 2-dimensional embedding cannot enter a 4-dimensional head."""
    head = linear_head()
    classifier = MessageClassifier(embedder=FakeEmbedder(vector=[1.0, 0.0]), head=head)
    try:
        asyncio.run(classifier.classify("hola?"))
    except ValueError as error:
        assert "dimensions" in str(error)
    else:
        raise AssertionError("dimension mismatch was not rejected")  # noqa: TRY003


def test_parse_margin_reads_stored_scores() -> None:
    """The stored margin round-trips through intent_scores_json."""
    import json

    payload = json.dumps({"question": 0.7, "margin": 0.2}, sort_keys=True)
    assert parse_margin(payload) == 0.2
    assert parse_margin(None) is None
    assert parse_margin("{not json") is None


def test_head_labels_order_is_fixed() -> None:
    """The exported head must use the canonical label order."""
    head = linear_head()
    assert head.labels == (
        IntentLabel.QUESTION,
        IntentLabel.KNOWLEDGE_UPDATE,
        IntentLabel.CORRECTION,
        IntentLabel.CHITCHAT,
    )


def test_intent_scores_best_tie_breaks_deterministically() -> None:
    """Equal probabilities pick the canonical label order."""
    assert IntentScores(0.25, 0.25, 0.25, 0.25).best()[0] is QUESTION


def test_custom_thresholds_shape_confidence() -> None:
    """Explicit thresholds can tighten or loosen the confidence policy."""
    classifier = MessageClassifier(
        embedder=FakeEmbedder(by_text={"mitjà": AMBIGUOUS}), head=linear_head()
    )
    classification = asyncio.run(classifier.classify("mitjà"))
    assert not classification.is_confident_with(0.60, 0.15)
