# SPDX-License-Identifier: MIT
"""Semantic message classifier over embedding similarity (plan §12).

No chat model is used here: each inbound text is embedded once, in the same
batch as a handful of Catalan prototype phrases per label, and scored by the
best cosine similarity per label. The scores are similarities, never
probabilities, and the labels are independent (a message can look like both a
question and a correction).

The policy is deliberately conservative: only a message that looks strongly
like chitchat *and* like nothing else is discarded. Everything else is kept
as context, and a reply that looks like an answer to a message that looks
like a question is matched into a question-answer pair.
"""

import math
from dataclasses import dataclass, field

from knowledge_bot.ports.embedder import Embedder

QUESTION = "question"
KNOWLEDGE_UPDATE = "knowledge_update"
CORRECTION = "correction"
CHITCHAT = "chitchat"

LABELS: tuple[str, str, str, str] = (
    QUESTION,
    KNOWLEDGE_UPDATE,
    CORRECTION,
    CHITCHAT,
)

PROTOTYPES: dict[str, tuple[str, ...]] = {
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

# Prototype vectors, embedded once per process and reused. Workers isolates
# keep module state across requests, so the steady state is one batched embed
# per classifier boot, not per message.
_PROTOTYPE_VECTORS: dict[tuple[str, ...], list[list[float]]] = {}


def _cosine(left: list[float], right: list[float]) -> float:
    """Return the cosine similarity of two vectors, or ``0.0`` when degenerate."""
    norm = math.sqrt(sum(value * value for value in left)) * math.sqrt(
        sum(value * value for value in right)
    )
    if norm <= 0.0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=False)) / norm


@dataclass(frozen=True, slots=True)
class IntentScores:
    """Best prototype similarity per label; similarities, not probabilities."""

    question: float = 0.0
    knowledge_update: float = 0.0
    correction: float = 0.0
    chitchat: float = 0.0

    def best(self) -> tuple[str, float]:
        """Return the ``(label, score)`` of the strongest label."""
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
        """Return the best score among the non-chitchat labels."""
        return max(self.question, self.knowledge_update, self.correction)


@dataclass(frozen=True, slots=True)
class MessageClassifier:
    """Score inbound messages against embedded intent prototypes."""

    embedder: Embedder
    prototypes: dict[str, tuple[str, ...]] = field(default_factory=lambda: PROTOTYPES)
    chitchat_discard_threshold: float = 0.80
    keep_signal_threshold: float = 0.45
    question_match_threshold: float = 0.60
    answer_match_threshold: float = 0.55

    async def classify(self, text: str) -> IntentScores:
        """Score a text against every prototype in a single embed batch.

        Args:
            text: The message text to score.

        Returns:
            The best similarity per label.
        """
        ordered = [
            (label, prototype)
            for label in LABELS
            for prototype in self.prototypes.get(label, ())
        ]
        key = tuple(prototype for _, prototype in ordered)
        cached = _PROTOTYPE_VECTORS.get(key)
        if cached is None:
            vectors = await self.embedder.embed(
                [text, *(prototype for _, prototype in ordered)]
            )
            message_vector, prototype_vectors = vectors[0], vectors[1:]
            _PROTOTYPE_VECTORS[key] = prototype_vectors
        else:
            message_vector = (await self.embedder.embed([text]))[0]
            prototype_vectors = cached
        best: dict[str, float] = dict.fromkeys(LABELS, 0.0)
        for (label, _), vector in zip(ordered, prototype_vectors, strict=True):
            best[label] = max(best[label], _cosine(message_vector, vector))
        return IntentScores(
            question=best[QUESTION],
            knowledge_update=best[KNOWLEDGE_UPDATE],
            correction=best[CORRECTION],
            chitchat=best[CHITCHAT],
        )

    def should_keep(self, scores: IntentScores) -> bool:
        """Return whether a message is worth keeping as context.

        Only a message that looks strongly like chitchat and like nothing
        else is discarded; any other signal keeps it.

        Args:
            scores: The intent scores of the message.

        Returns:
            ``False`` only for clear-cut chitchat.
        """
        if scores.chitchat < self.chitchat_discard_threshold:
            return True
        return scores.strongest_signal() >= self.keep_signal_threshold

    def is_question(self, scores: IntentScores) -> bool:
        """Return whether the scores clearly mark a question.

        Args:
            scores: The intent scores of the message.

        Returns:
            ``True`` when the question score clears the pair-matching bar.
        """
        return scores.question >= self.question_match_threshold

    def is_answer_like(self, scores: IntentScores) -> bool:
        """Return whether the scores look like an answer to a question.

        Args:
            scores: The intent scores of the message.

        Returns:
            ``True`` when an update or correction score clears the bar.
        """
        return (
            scores.knowledge_update >= self.answer_match_threshold
            or scores.correction >= self.answer_match_threshold
        )
