# SPDX-License-Identifier: MIT
"""Build, render, and schedule the periodic recap of asked questions.

The bot answers only when addressed, so unanswered questions (abstentions) are
collected and surfaced in the recap instead of being retried inline. The recap
is a pure transformation over stored answers; delivery goes through the
transport port.

Python Workers have no cron handler, so the recap is sent opportunistically:
whenever an event arrives, the service checks whether the recap is due and
posts it at most once per interval.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from knowledge_bot.domain.entities import BotAnswer
from knowledge_bot.domain.enums import AnswerMode
from knowledge_bot.domain.policies import is_recap_due
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.repositories import BotAnswerRepository, RecapStateRepository
from knowledge_bot.ports.transport import MessageTransport

DEFAULT_RECAP_LANGUAGE = "ca"

_RECAP_TEXTS: dict[str, dict[str, str]] = {
    "ca": {
        "header": "Resum de preguntes ({start} - {end})",
        "empty": "No hi ha preguntes en aquest període.",
        "pending": "(sense resposta encara)",
    },
    "es": {
        "header": "Resumen de preguntas ({start} - {end})",
        "empty": "No hay preguntas en este período.",
        "pending": "(sin respuesta todavía)",
    },
}


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


def render_recap(recap: Recap, *, language: str = DEFAULT_RECAP_LANGUAGE) -> str:
    """Render a recap as Telegram-friendly text.

    Args:
        recap: The recap to render.
        language: Language code for the fixed strings; unknown codes fall back
            to Catalan.

    Returns:
        A short text block listing each question and its answer, marking the
        ones that remain unanswered.
    """
    texts = _RECAP_TEXTS.get(language, _RECAP_TEXTS[DEFAULT_RECAP_LANGUAGE])
    header = texts["header"].format(
        start=f"{recap.window_start:%d/%m}",
        end=f"{recap.window_end:%d/%m}",
    )
    if not recap.entries:
        return f"{header}\n{texts['empty']}"
    lines = [header]
    for entry in recap.entries:
        if entry.answered:
            lines.append(f"\u2022 {entry.question}\n  \u2192 {entry.answer}")
        else:
            lines.append(f"\u2022 {entry.question}\n  \u2192 {texts['pending']}")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class RecapService:
    """Send the periodic recap opportunistically."""

    answers: BotAnswerRepository
    state: RecapStateRepository
    transport: MessageTransport
    clock: Clock
    enabled: bool = True
    interval_hours: int = 24
    language: str = DEFAULT_RECAP_LANGUAGE

    async def maybe_send(self, conversation_id: str) -> bool:
        """Send the recap when it is due.

        Args:
            conversation_id: The conversation to recap and post to.

        Returns:
            ``True`` when a recap was sent, ``False`` when it was disabled or
            not yet due.
        """
        if not self.enabled:
            return False
        now = self.clock.now()
        if not is_recap_due(
            await self.state.get_last_sent_at(conversation_id),
            now,
            interval_hours=self.interval_hours,
        ):
            return False
        window_start = now - timedelta(hours=self.interval_hours)
        recap = build_recap(
            await self.answers.list_between(window_start, now),
            window_start=window_start,
            window_end=now,
        )
        await self.transport.send_message(
            conversation_id,
            render_recap(recap, language=self.language),
        )
        await self.state.set_last_sent_at(conversation_id, now)
        return True
