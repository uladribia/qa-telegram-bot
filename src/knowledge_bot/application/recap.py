# SPDX-License-Identifier: MIT
"""Build and render the periodic recap of asked questions and answers.

The bot answers only when addressed, so unanswered questions (abstentions) are
collected and surfaced in the recap instead of being retried inline. The recap
is a pure transformation over stored answers; scheduling and delivery live in
the adapters.
"""

from dataclasses import dataclass
from datetime import datetime

from knowledge_bot.domain.entities import BotAnswer
from knowledge_bot.domain.enums import AnswerMode


@dataclass(frozen=True, slots=True)
class RecapEntry:
    """One question and the answer the bot gave, if any."""

    question: str
    answer: str | None
    answered: bool


@dataclass(frozen=True, slots=True)
class Recap:
    """A recap over a time window."""

    entries: list[RecapEntry]
    window_start: datetime
    window_end: datetime

    @property
    def unanswered(self) -> list[RecapEntry]:
        """Return the entries the bot could not answer."""
        return [entry for entry in self.entries if not entry.answered]


def build_recap(
    answers: list[BotAnswer],
    *,
    window_start: datetime,
    window_end: datetime,
) -> Recap:
    """Build a recap from the answers recorded in a window.

    Args:
        answers: Stored bot answers within the window.
        window_start: Inclusive start of the window.
        window_end: Exclusive end of the window.

    Returns:
        The recap, ordered by answer time.
    """
    entries = [
        RecapEntry(
            question=answer.question,
            answer=None
            if answer.answer_mode is AnswerMode.ABSTENTION
            else answer.answer,
            answered=answer.answer_mode is not AnswerMode.ABSTENTION,
        )
        for answer in sorted(answers, key=lambda item: item.created_at)
    ]
    return Recap(entries=entries, window_start=window_start, window_end=window_end)


def render_recap(recap: Recap) -> str:
    """Render a recap as Telegram-friendly text.

    Args:
        recap: The recap to render.

    Returns:
        A short text block listing each question and its answer, marking the
        ones that remain unanswered.
    """
    header = (
        f"Resum de preguntes ({recap.window_start:%d/%m} - {recap.window_end:%d/%m})"
    )
    if not recap.entries:
        return f"{header}\nNo hi ha preguntes en aquest període."
    lines = [header]
    for entry in recap.entries:
        if entry.answered:
            lines.append(f"\u2022 {entry.question}\n  \u2192 {entry.answer}")
        else:
            lines.append(f"\u2022 {entry.question}\n  \u2192 (sense resposta encara)")
    return "\n".join(lines)
