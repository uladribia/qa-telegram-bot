# SPDX-License-Identifier: MIT
"""Tests for the intake decision: one mode per conversation, four behaviours."""

from datetime import UTC, datetime

import pytest

from knowledge_bot.application.intake import (
    IntakeAction,
    decide_intake,
    is_addressed_to_bot,
)
from knowledge_bot.domain.enums import BotMode, ContentType
from knowledge_bot.models.messages import NormalizedMessage

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _message(**overrides: object) -> NormalizedMessage:
    data: dict = {
        "id": "m1",
        "source": {
            "id": "src:telegram:runtime",
            "kind": "telegram",
            "authority": 40,
        },
        "conversation_id": "c1",
        "sender_is_admin": False,
        "timestamp": NOW,
        "content_type": ContentType.TEXT,
        "text": "Quan entrenen?",
    }
    data.update(overrides)
    return NormalizedMessage.model_validate(data)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (BotMode.OFF, IntakeAction.IGNORE),
        (BotMode.SILENT, IntakeAction.INGEST),
        (BotMode.ACTIVE, IntakeAction.INGEST),
        (BotMode.PROACTIVE, IntakeAction.PROACTIVE),
    ],
)
def test_unaddressed_follows_the_mode(mode: BotMode, expected: IntakeAction) -> None:
    """A bare question is what tells the four modes apart."""
    assert decide_intake(_message(), mode) is expected


@pytest.mark.parametrize("mode", list(BotMode))
def test_addressed_is_answered_unless_the_group_is_off_or_muted(
    mode: BotMode,
) -> None:
    """Addressing the bot answers, except where the group answers nothing."""
    expected = {
        BotMode.OFF: IntakeAction.IGNORE,
        BotMode.SILENT: IntakeAction.INGEST,
        BotMode.ACTIVE: IntakeAction.ANSWER,
        BotMode.PROACTIVE: IntakeAction.ANSWER,
    }[mode]
    assert decide_intake(_message(mentions_bot=True), mode) is expected


def test_silent_stores_an_addressed_message_without_answering() -> None:
    """A muted group keeps everything and answers nothing."""
    assert decide_intake(_message(mentions_bot=True), BotMode.SILENT) is (
        IntakeAction.INGEST
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"mentions_bot": True},
        {"text": "/ask quan entrenen?"},
        {"is_direct_message": True},
        {"is_reply_to_bot": True},
    ],
)
def test_every_addressing_signal_answers(overrides: dict[str, object]) -> None:
    """Each explicit way of addressing the bot triggers an answer."""
    assert decide_intake(_message(**overrides), BotMode.ACTIVE) is (IntakeAction.ANSWER)
    assert is_addressed_to_bot(_message(**overrides))


def test_proactive_never_downgrades_an_addressed_message() -> None:
    """A mention in a proactive group is an ordinary answer, not a gamble."""
    assert decide_intake(_message(is_reply_to_bot=True), BotMode.PROACTIVE) is (
        IntakeAction.ANSWER
    )


def test_direct_messages_are_always_addressed() -> None:
    """A private message needs no mode to reach the bot."""
    assert is_addressed_to_bot(_message(is_direct_message=True))
