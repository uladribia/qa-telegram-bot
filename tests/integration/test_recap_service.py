# SPDX-License-Identifier: MIT
"""Integration tests for the opportunistic recap service (fake clock/transport)."""

from datetime import UTC, datetime, timedelta

from knowledge_bot.application.recap_service import RecapService
from knowledge_bot.domain.entities import BotAnswer
from knowledge_bot.domain.enums import AnswerMode
from tests.fakes.repositories import InMemoryBotAnswerRepository
from tests.fakes.support import (
    FrozenClock,
    InMemoryRecapStateRepository,
    RecordingTransport,
)

NOW = datetime(2026, 1, 2, 20, 0, tzinfo=UTC)


async def _service(
    *,
    enabled: bool = True,
    interval_hours: int = 24,
) -> tuple[RecapService, RecordingTransport, InMemoryRecapStateRepository, FrozenClock]:
    answers = InMemoryBotAnswerRepository()
    await answers.add(
        BotAnswer(
            id="a1",
            conversation_id="c1",
            question="Com es demana l'equipament?",
            answer="Després de provar talles.",
            answer_mode=AnswerMode.DIRECT_QA,
            created_at=NOW - timedelta(hours=1),
        )
    )
    state = InMemoryRecapStateRepository()
    transport = RecordingTransport()
    clock = FrozenClock(NOW)
    service = RecapService(
        answers=answers,
        state=state,
        transport=transport,
        clock=clock,
        enabled=enabled,
        interval_hours=interval_hours,
        language="ca",
    )
    return service, transport, state, clock


async def test_recap_is_sent_when_never_sent_before() -> None:
    """The first event after startup sends the recap."""
    service, transport, state, _ = await _service()
    assert await service.maybe_send("c1") is True
    assert len(transport.messages) == 1
    conversation_id, text = transport.messages[0]
    assert conversation_id == "c1"
    assert "Resum de preguntes" in text
    assert await state.get_last_sent_at("c1") == NOW


async def test_recap_is_not_sent_twice_within_the_interval() -> None:
    """The recap is rate-limited to once per interval."""
    service, transport, _, _ = await _service()
    await service.maybe_send("c1")
    assert await service.maybe_send("c1") is False
    assert len(transport.messages) == 1


async def test_recap_is_sent_again_after_the_interval() -> None:
    """Once the interval elapses, the next event sends a new recap."""
    service, transport, _, clock = await _service()
    await service.maybe_send("c1")
    clock.advance_to(NOW + timedelta(hours=25))
    assert await service.maybe_send("c1") is True
    assert len(transport.messages) == 2


async def test_disabled_recap_never_sends() -> None:
    """A disabled recap sends nothing and records nothing."""
    service, transport, state, _ = await _service(enabled=False)
    assert await service.maybe_send("c1") is False
    assert transport.messages == []
    assert await state.get_last_sent_at("c1") is None
