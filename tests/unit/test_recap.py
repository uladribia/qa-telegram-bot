# SPDX-License-Identifier: MIT
"""Tests for the periodic recap builder and renderer."""

import asyncio
from datetime import UTC, datetime, timedelta

from knowledge_bot.application.budget import AiBudget
from knowledge_bot.application.recap_service import (
    Recap,
    RecapService,
    build_recap,
    render_recap,
)
from knowledge_bot.domain.entities import BotAnswer, Feedback, Message
from knowledge_bot.domain.enums import AnswerMode, ContentType, FeedbackStatus
from tests.fakes.repositories import (
    InMemoryBotAnswerRepository,
    InMemoryConversationRepository,
    InMemoryFeedbackRepository,
    InMemoryMessageRepository,
)
from tests.fakes.support import (
    FrozenClock,
    InMemoryAiUsageRepository,
    InMemoryRecapStateRepository,
    RecordingTransport,
)

START = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
END = START + timedelta(days=1)


def _answer(
    answer_id: str,
    question: str,
    answer: str,
    *,
    mode: AnswerMode = AnswerMode.DIRECT_QA,
    minutes: int = 0,
) -> BotAnswer:
    return BotAnswer(
        id=answer_id,
        conversation_id="c1",
        question=question,
        answer=answer,
        answer_mode=mode,
        created_at=START + timedelta(minutes=minutes),
    )


def test_build_recap_orders_by_time() -> None:
    """Entries are ordered by when the answer was produced."""
    recap = build_recap(
        [
            _answer("a2", "Pregunta 2", "Resposta 2", minutes=30),
            _answer("a1", "Pregunta 1", "Resposta 1", minutes=10),
        ],
        window_start=START,
        window_end=END,
    )
    assert [entry.question for entry in recap.entries] == ["Pregunta 1", "Pregunta 2"]


def test_abstentions_are_unanswered() -> None:
    """An abstention has no answer and is reported as unanswered."""
    recap = build_recap(
        [
            _answer(
                "a1",
                "Que? ",
                "No tinc prou informaci\u00f3.",
                mode=AnswerMode.ABSTENTION,
            ),
            _answer("a2", "Com?", "Aix\u00ed.", mode=AnswerMode.DIRECT_QA),
        ],
        window_start=START,
        window_end=END,
    )
    assert len(recap.unanswered) == 1
    assert recap.unanswered[0].question == "Que? "
    assert recap.unanswered[0].answer is None


def test_render_recap_lists_questions_and_answers() -> None:
    """Rendering includes each question, its answer, and the header."""
    recap = build_recap(
        [
            _answer(
                "a1", "Com es demana l'equipament?", "Despr\u00e9s de provar talles."
            ),
            _answer("a2", "Qui vindr\u00e0?", "", mode=AnswerMode.ABSTENTION),
        ],
        window_start=START,
        window_end=END,
    )
    text = render_recap(recap)
    assert "Resum de preguntes" in text
    assert "Com es demana l'equipament?" in text
    assert "Despr\u00e9s de provar talles." in text
    assert "(sense resposta encara)" in text


def test_render_recap_handles_empty_window() -> None:
    """An empty window produces a clear message."""
    recap = build_recap([], window_start=START, window_end=END)
    assert isinstance(recap, Recap)
    assert "No hi ha preguntes" in render_recap(recap)


def test_render_recap_defaults_to_catalan() -> None:
    """The default language is Catalan."""
    recap = build_recap([], window_start=START, window_end=END)
    assert render_recap(recap) == render_recap(recap, language="ca")


def test_render_recap_supports_spanish() -> None:
    """The recap language is configurable."""
    recap = build_recap(
        [_answer("a1", "Qui?", "")],
        window_start=START,
        window_end=END,
    )
    spanish = render_recap(recap, language="es")
    assert "Resumen de preguntas" in spanish
    assert "Resum de preguntes" not in spanish


def test_render_recap_unknown_language_falls_back_to_catalan() -> None:
    """An unknown language code falls back to Catalan."""
    recap = build_recap([], window_start=START, window_end=END)
    assert render_recap(recap, language="fr") == render_recap(recap, language="ca")


def _message(
    message_id: str, *, intent: str | None = None, pair: bool = False
) -> Message:
    return Message(
        id=message_id,
        source_id="telegram",
        conversation_id="c1",
        content_type=ContentType.TEXT,
        sent_at=START,
        created_at=START,
        text="Hola",
        intent_label=intent,
        intent_score=0.9 if intent is not None else None,
        context_question="Qui?" if pair else None,
    )


def test_maybe_send_appends_the_activity_footer() -> None:
    """The recap reports context, spend, and question outcomes."""
    answers = InMemoryBotAnswerRepository()
    feedback_repo = InMemoryFeedbackRepository()
    messages = InMemoryMessageRepository()
    usage = InMemoryAiUsageRepository()
    clock = FrozenClock(END)
    usage.seed(END.strftime("%Y-%m-%d"), 120.0, 3)
    asyncio.run(answers.add(_answer("a1", "Qui?", "Tu.", minutes=10)))
    asyncio.run(
        answers.add(
            _answer("a2", "Quan?", "Demà.", mode=AnswerMode.SYNTHESIS, minutes=20)
        )
    )
    asyncio.run(
        answers.add(_answer("a3", "On?", "...", mode=AnswerMode.ABSTENTION, minutes=30))
    )
    asyncio.run(
        feedback_repo.add(
            Feedback(
                id="fb:a2",
                bot_answer_id="a2",
                status=FeedbackStatus.PENDING_ADMIN,
                created_at=START + timedelta(minutes=25),
            )
        )
    )
    asyncio.run(messages.add(_message("m1", intent="knowledge_update")))
    asyncio.run(messages.add(_message("m2", intent="question", pair=True)))
    transport = RecordingTransport()
    service = RecapService(
        answers=answers,
        conversations=InMemoryConversationRepository(),
        state=InMemoryRecapStateRepository(),
        transport=transport,
        clock=clock,
        admin_user_id="1",
        interval_hours=24,
        budget=AiBudget(usage=usage, clock=clock),
        feedback=feedback_repo,
        messages=messages,
    )
    assert asyncio.run(service.maybe_send())
    (chat, text) = transport.messages[0]
    assert chat == "1"
    assert "context capturat: 2 missatges (1 parelles pregunta-resposta)" in text
    assert "120 / 10000 neurones en 3 crides" in text
    assert "preguntes: 3 (ben resoltes: 1" in text
    assert "marcades com a incorrectes: 1" in text
    assert "sense resposta: 1)" in text
