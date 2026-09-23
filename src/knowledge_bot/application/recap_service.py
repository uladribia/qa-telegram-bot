# SPDX-License-Identifier: MIT
"""Build, render, and schedule the daily question summary for the admin.

Groups never receive summaries. The admin receives one recap per interval
covering every conversation's questions, each tagged with the group it came
from, so unanswered questions are surfaced without interrupting any chat. The
recap is a pure transformation over stored answers; delivery goes through the
transport port.

Python Workers have no cron handler, so the recap is sent opportunistically:
whenever an event arrives, the service checks whether the recap is due and
posts it at most once per interval.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from knowledge_bot.application.budget import AiBudget
from knowledge_bot.domain.entities import BotAnswer
from knowledge_bot.domain.enums import AnswerMode
from knowledge_bot.domain.policies import is_recap_due
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.repositories import (
    BotAnswerRepository,
    ConversationRepository,
    FeedbackRepository,
    MessageRepository,
    RecapStateRepository,
)
from knowledge_bot.ports.transport import MessageTransport

DEFAULT_RECAP_LANGUAGE = "ca"
ADMIN_STATE_KEY = "admin"

_RECAP_TEXTS: dict[str, dict[str, str]] = {
    "ca": {
        "header": "Resum de preguntes ({start} - {end})",
        "empty": "No hi ha preguntes en aquest període.",
        "pending": "(sense resposta encara)",
        "activity": (
            "\U0001f4ca Activitat: context capturat: {ingested} missatges"
            " ({paired} parelles pregunta-resposta);"
            " IA avui: {neurons:.0f} / {limit:.0f} neurones en {calls} crides;"
            " preguntes: {asked} (ben resoltes: {correct},"
            " marcades com a incorrectes: {incorrect},"
            " sense resposta: {unsolved})"
        ),
    },
    "es": {
        "header": "Resumen de preguntas ({start} - {end})",
        "empty": "No hay preguntas en este período.",
        "pending": "(sin respuesta todavía)",
        "activity": (
            "\U0001f4ca Actividad: contexto capturado: {ingested} mensajes"
            " ({paired} pares pregunta-respuesta);"
            " IA hoy: {neurons:.0f} / {limit:.0f} neuronas en {calls} llamadas;"
            " preguntas: {asked} (bien resueltas: {correct},"
            " marcadas como incorrectas: {incorrect},"
            " sin respuesta: {unsolved})"
        ),
    },
}


@dataclass(frozen=True, slots=True)
class ActivityStats:
    """Listener, spend, and question outcomes over a recap window.

    ``incorrect`` counts answered questions flagged as wrong at least once;
    ``correct`` is answered and never flagged. Both are observed proxies,
    not quality judgements.
    """

    ingested: int
    paired: int
    neurons: float
    neuron_limit: float
    calls: int
    asked: int
    correct: int
    incorrect: int
    unsolved: int


def render_activity(
    stats: ActivityStats, *, language: str = DEFAULT_RECAP_LANGUAGE
) -> str:
    """Render the activity footer of a recap.

    Args:
        stats: The window statistics.
        language: Language code for the fixed strings; unknown codes fall
            back to Catalan.

    Returns:
        A one-paragraph activity summary.
    """
    texts = _RECAP_TEXTS.get(language, _RECAP_TEXTS[DEFAULT_RECAP_LANGUAGE])
    return texts["activity"].format(
        ingested=stats.ingested,
        paired=stats.paired,
        neurons=stats.neurons,
        limit=stats.neuron_limit,
        calls=stats.calls,
        asked=stats.asked,
        correct=stats.correct,
        incorrect=stats.incorrect,
        unsolved=stats.unsolved,
    )


@dataclass(frozen=True, slots=True)
class RecapEntry:
    """One question and the answer the bot gave, if any."""

    question: str
    answer: str | None
    answered: bool
    group_label: str | None = None


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
    group_labels: dict[str, str] | None = None,
) -> Recap:
    """Build a recap from the answers recorded in a window.

    Args:
        answers: Stored bot answers within the window.
        window_start: Inclusive start of the window.
        window_end: Exclusive end of the window.
        group_labels: Optional ``conversation_id -> display label`` mapping,
            used to tag each entry with the group it came from.

    Returns:
        The recap, ordered by answer time.
    """
    labels = group_labels or {}
    entries = [
        RecapEntry(
            question=answer.question,
            answer=None
            if answer.answer_mode is AnswerMode.ABSTENTION
            else answer.answer,
            answered=answer.answer_mode is not AnswerMode.ABSTENTION,
            group_label=labels.get(answer.conversation_id),
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
        prefix = f"[{entry.group_label}] " if entry.group_label else ""
        if entry.answered:
            lines.append(f"\u2022 {prefix}{entry.question}\n  \u2192 {entry.answer}")
        else:
            lines.append(
                f"\u2022 {prefix}{entry.question}\n  \u2192 {texts['pending']}"
            )
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class RecapService:
    """Send the daily question summary to the admin, opportunistically."""

    answers: BotAnswerRepository
    conversations: ConversationRepository
    state: RecapStateRepository
    transport: MessageTransport
    clock: Clock
    admin_user_id: str | None = None
    enabled: bool = True
    interval_hours: int = 24
    language: str = DEFAULT_RECAP_LANGUAGE
    budget: AiBudget | None = None
    feedback: FeedbackRepository | None = None
    messages: MessageRepository | None = None

    async def maybe_send(self) -> bool:
        """Send the admin recap when it is due.

        Returns:
            ``True`` when a recap was sent, ``False`` when it was disabled,
            missing an admin, or not yet due.
        """
        if not self.enabled or not self.admin_user_id:
            return False
        now = self.clock.now()
        if not is_recap_due(
            await self.state.get_last_sent_at(ADMIN_STATE_KEY),
            now,
            interval_hours=self.interval_hours,
        ):
            return False
        window_start = now - timedelta(hours=self.interval_hours)
        answers = await self.answers.list_between(window_start, now)
        labels = {
            answer.conversation_id: label
            for answer in answers
            if (conversation := await self.conversations.get(answer.conversation_id))
            and (label := conversation.title or answer.conversation_id)
        }
        recap = build_recap(
            answers,
            window_start=window_start,
            window_end=now,
            group_labels=labels,
        )
        text = render_recap(recap, language=self.language)
        stats = await self._activity(window_start, now, answers)
        if stats is not None:
            text = f"{text}\n\n{render_activity(stats, language=self.language)}"
        await self.transport.send_message(self.admin_user_id, text)
        await self.state.set_last_sent_at(ADMIN_STATE_KEY, now)
        return True

    async def _activity(
        self, window_start: datetime, now: datetime, answers: list[BotAnswer]
    ) -> ActivityStats | None:
        """Build the activity stats, or ``None`` when stores are unwired."""
        if self.budget is None or self.feedback is None or self.messages is None:
            return None
        flagged = {
            item.bot_answer_id
            for item in await self.feedback.list_between(window_start, now)
        }
        solved = [
            answer
            for answer in answers
            if answer.answer_mode in (AnswerMode.DIRECT_QA, AnswerMode.SYNTHESIS)
        ]
        incorrect = sum(1 for answer in solved if answer.id in flagged)
        ingested, paired = await self.messages.listener_stats_between(window_start, now)
        neurons, calls = await self.budget.usage_today()
        return ActivityStats(
            ingested=ingested,
            paired=paired,
            neurons=neurons,
            neuron_limit=self.budget.daily_neurons,
            calls=calls,
            asked=len(answers),
            correct=len(solved) - incorrect,
            incorrect=incorrect,
            unsolved=len(answers) - len(solved),
        )
