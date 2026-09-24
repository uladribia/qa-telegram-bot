# SPDX-License-Identifier: MIT
"""Tests for the intake decision (answer vs. listen vs. ignore)."""

from datetime import UTC, datetime

from knowledge_bot.application.intake import IntakeAction, decide_intake
from knowledge_bot.contracts.messages import NormalizedMessage
from knowledge_bot.domain.enums import ContentType

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


def test_addressed_messages_are_answered() -> None:
    """Every explicit addressing signal triggers an answer."""
    assert decide_intake(_message(mentions_bot=True)) is IntakeAction.ANSWER
    assert decide_intake(_message(text="/ask quan entrenen?")) is IntakeAction.ANSWER
    assert decide_intake(_message(is_direct_message=True)) is IntakeAction.ANSWER
    assert decide_intake(_message(is_reply_to_bot=True)) is IntakeAction.ANSWER


def test_unaddressed_is_ignored_by_default() -> None:
    """The background listener is off, so a bare question is ignored."""
    assert decide_intake(_message()) is IntakeAction.IGNORE
    assert (
        decide_intake(_message(), background_listener_enabled=False)
        is IntakeAction.IGNORE
    )


def test_unaddressed_is_ingested_when_listening() -> None:
    """With the listener on, unaddressed traffic is stored but not answered."""
    assert (
        decide_intake(_message(), background_listener_enabled=True)
        is IntakeAction.INGEST
    )


def test_addressed_is_answered_even_when_listening() -> None:
    """Listening does not change the answer behaviour."""
    action = decide_intake(
        _message(mentions_bot=True), background_listener_enabled=True
    )
    assert action is IntakeAction.ANSWER
