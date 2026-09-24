# SPDX-License-Identifier: MIT
"""Linear intent classification for background messages.

One multinomial logistic-regression head over the message embedding replaces
the former prototype scoring. The head is trained locally (see
``scripts/train_classifier.py``) and exported to ``data/classifier/model.json``;
the runtime only applies the exported coefficients with plain Python.
"""

import json
import math
from dataclasses import dataclass

from knowledge_bot.domain.enums import IntentLabel
from knowledge_bot.ports.embedder import Embedder

QUESTION = IntentLabel.QUESTION
KNOWLEDGE_UPDATE = IntentLabel.KNOWLEDGE_UPDATE
CORRECTION = IntentLabel.CORRECTION
CHITCHAT = IntentLabel.CHITCHAT

LABELS: tuple[IntentLabel, ...] = (
    QUESTION,
    KNOWLEDGE_UPDATE,
    CORRECTION,
    CHITCHAT,
)

_ANSWER_LABELS = frozenset({KNOWLEDGE_UPDATE, CORRECTION})

_ACKNOWLEDGEMENTS = frozenset(
    {"ok", "gràcies", "gracias", "perfecte", "perfecto", "d'acord"}
)


@dataclass(frozen=True, slots=True)
class ClassifierHead:
    """Exported linear coefficients in a fixed label order."""

    labels: tuple[IntentLabel, ...]
    coef: tuple[tuple[float, ...], ...]
    intercept: tuple[float, ...]

    def logits(self, embedding: tuple[float, ...]) -> list[float]:
        """Return one logit per label for one message embedding."""
        if len(embedding) != len(self.coef[0]):
            message = "classifier embedding dimensions do not match the head"
            raise ValueError(message)
        return [
            sum(
                weight * value for weight, value in zip(weights, embedding, strict=True)
            )
            + bias
            for weights, bias in zip(self.coef, self.intercept, strict=True)
        ]


@dataclass(frozen=True, slots=True)
class IntentScores:
    """Predicted probability per intent label."""

    question: float = 0.0
    knowledge_update: float = 0.0
    correction: float = 0.0
    chitchat: float = 0.0

    def best(self) -> tuple[IntentLabel, float]:
        """Return the strongest label and its probability."""
        ranked = sorted(
            (
                (QUESTION, self.question),
                (KNOWLEDGE_UPDATE, self.knowledge_update),
                (CORRECTION, self.correction),
                (CHITCHAT, self.chitchat),
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        return ranked[0]


@dataclass(frozen=True, slots=True)
class Classification:
    """One classification result and its reusable message embedding."""

    scores: IntentScores
    best_label: IntentLabel
    best_score: float
    margin: float
    embedding: tuple[float, ...]

    @property
    def confident(self) -> bool:
        """Whether the top label clears the operational confidence policy."""
        return self.best_score >= 0.60 and self.margin >= 0.15

    def is_confident_with(
        self, confidence_threshold: float, margin_threshold: float
    ) -> bool:
        """Apply explicit confidence and margin thresholds."""
        return (
            self.best_score >= confidence_threshold and self.margin >= margin_threshold
        )


@dataclass(frozen=True, slots=True)
class MessageClassifier:
    """Classify messages with a softmax over the exported linear head."""

    embedder: Embedder
    head: ClassifierHead
    confidence_threshold: float = 0.60
    margin_threshold: float = 0.15

    async def classify(self, text: str) -> Classification:
        """Classify text, using no model call for deterministic prefilter cases."""
        normalized = " ".join(text.split()).casefold()
        alphanumeric = [character for character in normalized if character.isalnum()]
        if not alphanumeric or len(alphanumeric) < 2 or normalized in _ACKNOWLEDGEMENTS:
            return _prefilter_classification()
        vectors = await self.embedder.embed([text])
        embedding = tuple(vectors[0])
        scores = _softmax(self.head.logits(embedding))
        top_two = sorted(scores, reverse=True)
        margin = top_two[0] - top_two[1]
        best_label, best_score = IntentScores(*scores).best()
        return Classification(
            scores=IntentScores(*scores),
            best_label=best_label,
            best_score=best_score,
            margin=margin,
            embedding=embedding,
        )

    def is_question(self, classification: Classification) -> bool:
        """Whether the message is a confident question (pending-question rule)."""
        return (
            not self._ambiguous(classification)
            and classification.best_label is QUESTION
        )

    def is_answer_like(self, classification: Classification) -> bool:
        """Whether the message is a confident factual update or correction."""
        return (
            not self._ambiguous(classification)
            and classification.best_label in _ANSWER_LABELS
        )

    def _ambiguous(self, classification: Classification) -> bool:
        return not classification.is_confident_with(
            self.confidence_threshold, self.margin_threshold
        )


def _softmax(logits: list[float]) -> tuple[float, float, float, float]:
    """Return label probabilities from the linear logits."""
    peak = max(logits)
    exps = [math.exp(value - peak) for value in logits]
    total = sum(exps)
    first, second, third, fourth = (value / total for value in exps)
    return first, second, third, fourth


def _prefilter_classification() -> Classification:
    return Classification(
        scores=IntentScores(chitchat=1.0),
        best_label=CHITCHAT,
        best_score=1.0,
        margin=1.0,
        embedding=(),
    )


def parse_margin(intent_scores_json: str | None) -> float | None:
    """Return the stored top1-top2 margin of a classified message."""
    if not intent_scores_json:
        return None
    try:
        payload = json.loads(intent_scores_json)
    except ValueError:
        return None
    margin = payload.get("margin") if isinstance(payload, dict) else None
    return float(margin) if isinstance(margin, (int, float)) else None


def message_is_confident(
    intent_label: str | None,
    intent_score: float | None,
    intent_scores_json: str | None,
    *,
    confidence_threshold: float,
    margin_threshold: float,
) -> bool:
    """Whether a stored message cleared the confidence policy at ingest time.

    Questions, updates, and corrections stored with a low margin stay stored
    but are treated as ambiguous: they never become pending questions or
    factual evidence.
    """
    if intent_label is None or intent_score is None:
        return False
    margin = parse_margin(intent_scores_json)
    if margin is None:
        return False
    return intent_score >= confidence_threshold and margin >= margin_threshold
