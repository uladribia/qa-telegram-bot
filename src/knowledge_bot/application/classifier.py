# SPDX-License-Identifier: MIT
"""Embedding-prototype classification for background messages."""

import math
from dataclasses import dataclass, field

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

PROTOTYPES: dict[IntentLabel, tuple[str, ...]] = {
    QUESTION: (
        "a quina hora és l'entrenament de demà?",
        "què hem de portar al partit de dissabte?",
        "on es compra l'equipament del club?",
        "qui és l'entrenador dels minis aquest any?",
    ),
    KNOWLEDGE_UPDATE: (
        "recordeu que demà no hi ha entrenament",
        "la reunió de pares serà a les cinc a la sala gran",
        "el partit de diumenge es juga al camp de dalt",
        "han publicat els horaris nous al web del club",
    ),
    CORRECTION: (
        "no, finalment l'entrenament és dijous",
        "han canviat l'hora: serà a les sis",
        "m'he equivocat, el partit és diumenge no dissabte",
        "correcció: el preu són vint euros, no quinze",
    ),
    CHITCHAT: (
        "gràcies!",
        "perfecte, ens veiem demà",
        "😂😂😂",
        "d'acord, cap problema",
    ),
}

_ACKNOWLEDGEMENTS = frozenset(
    {"ok", "gràcies", "gracias", "perfecte", "perfecto", "d'acord"}
)


def _cosine(left: list[float], right: list[float]) -> float:
    """Return cosine similarity for equally sized, non-zero vectors."""
    norm = math.sqrt(sum(value * value for value in left)) * math.sqrt(
        sum(value * value for value in right)
    )
    if norm <= 0.0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / norm


@dataclass(frozen=True, slots=True)
class IntentScores:
    """Best prototype similarity per intent label."""

    question: float = 0.0
    knowledge_update: float = 0.0
    correction: float = 0.0
    chitchat: float = 0.0

    def best(self) -> tuple[IntentLabel, float]:
        """Return the strongest intent label and score."""
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

    def strongest_signal(self) -> float:
        """Return the strongest non-chitchat score."""
        return max(self.question, self.knowledge_update, self.correction)


@dataclass(frozen=True, slots=True)
class Classification:
    """One classification result and its reusable message embedding."""

    scores: IntentScores
    best_label: IntentLabel
    best_score: float
    embedding: tuple[float, ...]


@dataclass(slots=True)
class MessageClassifier:
    """Score messages against per-classifier cached prototype embeddings."""

    embedder: Embedder
    prototypes: dict[IntentLabel, tuple[str, ...]] = field(
        default_factory=lambda: PROTOTYPES
    )
    chitchat_discard_threshold: float = 0.80
    keep_signal_threshold: float = 0.45
    question_match_threshold: float = 0.60
    answer_match_threshold: float = 0.55
    _prototype_vectors: list[list[float]] | None = field(default=None, init=False)

    async def classify(self, text: str) -> Classification:
        """Classify text, using no model call for deterministic prefilter cases."""
        normalized = " ".join(text.split()).casefold()
        alphanumeric = [character for character in normalized if character.isalnum()]
        if not alphanumeric:
            return _prefilter_classification()
        if len(alphanumeric) < 2 or normalized in _ACKNOWLEDGEMENTS:
            return _prefilter_classification()

        ordered = [
            (label, prototype)
            for label in LABELS
            for prototype in self.prototypes.get(label, ())
        ]
        if self._prototype_vectors is None:
            vectors = await self.embedder.embed(
                [text, *(prototype for _, prototype in ordered)]
            )
            message_vector, self._prototype_vectors = vectors[0], vectors[1:]
        else:
            message_vector = (await self.embedder.embed([text]))[0]
        _validate_vectors([message_vector, *self._prototype_vectors])
        best = dict.fromkeys(LABELS, 0.0)
        for (label, _), vector in zip(ordered, self._prototype_vectors, strict=True):
            best[label] = max(best[label], _cosine(message_vector, vector))
        scores = IntentScores(
            question=best[QUESTION],
            knowledge_update=best[KNOWLEDGE_UPDATE],
            correction=best[CORRECTION],
            chitchat=best[CHITCHAT],
        )
        best_label, best_score = scores.best()
        return Classification(
            scores=scores,
            best_label=best_label,
            best_score=best_score,
            embedding=tuple(message_vector),
        )

    def should_keep(self, scores: IntentScores) -> bool:
        """Return whether a classified message has useful signal."""
        if scores.chitchat < self.chitchat_discard_threshold:
            return True
        return scores.strongest_signal() >= self.keep_signal_threshold

    def is_question(self, scores: IntentScores) -> bool:
        """Return whether the scores clearly mark a question."""
        return scores.question >= self.question_match_threshold

    def is_answer_like(self, scores: IntentScores) -> bool:
        """Return whether the scores look like an answer to a question."""
        return (
            scores.knowledge_update >= self.answer_match_threshold
            or scores.correction >= self.answer_match_threshold
        )


def _prefilter_classification() -> Classification:
    scores = IntentScores(chitchat=1.0)
    return Classification(
        scores=scores,
        best_label=CHITCHAT,
        best_score=1.0,
        embedding=(),
    )


def _validate_vectors(vectors: list[list[float]]) -> None:
    if not vectors:
        message = "classifier received no vectors"
        raise ValueError(message)
    dimensions = len(vectors[0])
    if dimensions == 0 or any(len(vector) != dimensions for vector in vectors):
        message = "classifier embedding dimensions do not match"
        raise ValueError(message)
